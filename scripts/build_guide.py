#!/usr/bin/env python3
"""Build one XMLTV guide for an iptv-org country playlist.

For each playlist tvg-id, candidates come from iptv-org/epg ``sites/*/*.channels.xml``
(the same mapping published as guides.json). An exact id, including the feed tag
(``CNN.us@SD``), is preferred over another feed of the same channel. Among those,
sites that have produced usable listings are tried first. ``tvtv.us`` is skipped
because its guide requests answer 404. Channels that come back with no programmes
are retried on the next site. A fallback feed is relabeled with the playlist
tvg-id so players can match the m3u.
"""

from __future__ import annotations

import argparse
import gzip
import json
import os
import re
import signal
import subprocess
import sys
import tempfile
import time
import traceback
import xml.etree.ElementTree as ET
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from urllib.request import Request, urlopen

PREFERRED_SITES = [
    "i.mjh.nz",
    "tvpassport.com",
    "plex.tv",
    "tvguide.com",
    "gatotv.com",
    "mi.tv",
]
# Guide requests to tvtv.us have been returning 404, so never select it.
EXCLUDED_SITES = {"tvtv.us"}

PREFERRED_LANG = {"us": "en", "mx": "es"}
PLAYLISTS = {
    "us": "https://iptv-org.github.io/iptv/countries/us.m3u",
    "mx": "https://iptv-org.github.io/iptv/countries/mx.m3u",
}
# When several feeds of the same channel exist, prefer a national/timezone feed
# over an arbitrary local call sign.
FEED_ORDER = ["SD", "HD", "East", "Eastern", "West", "Pacific", "Central", "Mountain"]

USER_AGENT = "EPG-guide-builder/1.0"
TVG_RE = re.compile(r'\btvg-id="([^"]*)"')


@dataclass
class PlaylistChannel:
    tvg_id: str
    name: str
    order: int


@dataclass
class SourceChannel:
    site: str
    site_id: str
    lang: str
    xmltv_id: str
    name: str


@dataclass
class GrabbedChannel:
    tvg_id: str
    name: str
    order: int
    site: str
    channel_elem: ET.Element
    programmes: list[ET.Element] = field(default_factory=list)


def base_id(xmltv_id: str) -> str:
    return xmltv_id.split("@", 1)[0]


def site_rank(site: str) -> int:
    try:
        return PREFERRED_SITES.index(site)
    except ValueError:
        return 1_000


def feed_rank(xmltv_id: str) -> int:
    if "@" not in xmltv_id:
        return 20
    feed = xmltv_id.split("@", 1)[1]
    try:
        return FEED_ORDER.index(feed)
    except ValueError:
        return 50


def candidate_key(entry: SourceChannel, wanted: str, lang: str) -> tuple:
    exact = 0 if entry.xmltv_id == wanted else 1
    lang_rank = 0 if entry.lang == lang else (1 if entry.lang else 2)
    return (
        exact,
        site_rank(entry.site),
        lang_rank,
        feed_rank(entry.xmltv_id),
        entry.site,
        entry.xmltv_id,
        entry.site_id,
    )


def rank_candidates(
    entries: list[SourceChannel], wanted: str, lang: str
) -> list[SourceChannel]:
    """One source per site, exact feed first, then preferred sites."""
    usable = [
        entry
        for entry in entries
        if entry.site and entry.site_id and entry.site not in EXCLUDED_SITES
    ]
    ordered = sorted(usable, key=lambda entry: candidate_key(entry, wanted, lang))
    best_by_site: dict[str, SourceChannel] = {}
    for entry in ordered:
        best_by_site.setdefault(entry.site, entry)
    return sorted(best_by_site.values(), key=lambda entry: candidate_key(entry, wanted, lang))


def parse_playlist(text: str) -> list[PlaylistChannel]:
    channels: list[PlaylistChannel] = []
    seen: set[str] = set()
    for raw in text.splitlines():
        line = raw.strip()
        if not line.startswith("#EXTINF:"):
            continue
        # Attribute values can contain commas; the display name follows the
        # first comma that is not inside quotes.
        body = line.split(":", 1)[1]
        in_quotes = False
        split_at = None
        for index, char in enumerate(body):
            if char == '"':
                in_quotes = not in_quotes
            elif char == "," and not in_quotes:
                split_at = index
                break
        if split_at is None:
            continue
        attrs = body[:split_at]
        name = body[split_at + 1 :].strip() or "Unknown"
        match = TVG_RE.search(attrs)
        if not match:
            continue
        tvg_id = match.group(1).strip()
        if not tvg_id or tvg_id in seen:
            continue
        seen.add(tvg_id)
        channels.append(PlaylistChannel(tvg_id, name, len(channels)))
    return channels


def fetch_text(url: str, timeout: int = 60) -> str:
    request = Request(url, headers={"User-Agent": USER_AGENT})
    last_error: Exception | None = None
    for attempt in range(3):
        try:
            with urlopen(request, timeout=timeout) as response:
                return response.read().decode("utf-8", errors="replace")
        except Exception as error:  # noqa: BLE001 - retry any transient fetch error
            last_error = error
            time.sleep(2 * (attempt + 1))
    raise RuntimeError(f"failed to download {url}: {last_error}")


def load_source_index(epg_dir: Path) -> dict[str, list[SourceChannel]]:
    sites_dir = epg_dir / "sites"
    if not sites_dir.is_dir():
        raise SystemExit(f"epg sites directory not found: {sites_dir}")
    by_base: dict[str, list[SourceChannel]] = defaultdict(list)
    files = sorted(sites_dir.glob("*/*.channels.xml"))
    print(f"indexing {len(files)} channel files from {sites_dir}", flush=True)
    for path in files:
        config = path.parent / f"{path.parent.name}.config.js"
        if not config.is_file():
            continue
        try:
            root = ET.parse(path).getroot()
        except ET.ParseError as error:
            print(f"skipping unreadable {path}: {error}", file=sys.stderr)
            continue
        default_site = root.get("site") or path.parent.name
        for node in root.findall("channel"):
            xmltv_id = (node.get("xmltv_id") or "").strip()
            site = (node.get("site") or default_site).strip()
            site_id = (node.get("site_id") or "").strip()
            if not xmltv_id or not site or not site_id or site_id == "#":
                continue
            if site in EXCLUDED_SITES:
                continue
            by_base[base_id(xmltv_id)].append(
                SourceChannel(
                    site=site,
                    site_id=site_id,
                    lang=(node.get("lang") or "").strip(),
                    xmltv_id=xmltv_id,
                    name=(node.text or "").strip() or xmltv_id,
                )
            )
    print(f"indexed {sum(len(v) for v in by_base.values())} source channels", flush=True)
    return by_base


def select_candidates(
    playlist: list[PlaylistChannel],
    by_base: dict[str, list[SourceChannel]],
    lang: str,
) -> dict[str, list[SourceChannel]]:
    selected: dict[str, list[SourceChannel]] = {}
    for channel in playlist:
        entries = by_base.get(base_id(channel.tvg_id), [])
        # Same channel id only. ``CNN.us`` must not match ``CNNInternational.us``.
        same_channel = [entry for entry in entries if base_id(entry.xmltv_id) == base_id(channel.tvg_id)]
        selected[channel.tvg_id] = rank_candidates(same_channel, channel.tvg_id, lang)
    return selected


def xml_escape(value: str) -> str:
    return (
        value.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def write_channels_xml(path: Path, rows: list[tuple[PlaylistChannel, SourceChannel]]) -> None:
    lines = ['<?xml version="1.0" encoding="UTF-8"?>', "<channels>"]
    for playlist_channel, source in rows:
        # xmltv_id is the playlist id even when the source feed is different,
        # so the guide lines up with the m3u. site_id still selects the feed.
        lines.append(
            "  <channel"
            f' site="{xml_escape(source.site)}"'
            f' site_id="{xml_escape(source.site_id)}"'
            f' lang="{xml_escape(source.lang)}"'
            f' xmltv_id="{xml_escape(playlist_channel.tvg_id)}"'
            f">{xml_escape(playlist_channel.name)}</channel>"
        )
    lines.append("</channels>")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def run_grab(
    epg_dir: Path,
    channels_file: Path,
    output_file: Path,
    days: int,
    max_connections: int,
    timeout_ms: int,
    grab_timeout_sec: int,
) -> int:
    output_file.parent.mkdir(parents=True, exist_ok=True)
    command = [
        "npm",
        "run",
        "grab",
        "---",
        f"--channels={channels_file}",
        f"--output={output_file}",
        "--gzip",
        f"--days={days}",
        f"--maxConnections={max_connections}",
        f"--timeout={timeout_ms}",
    ]
    print(" ".join(command), flush=True)
    env = os.environ.copy()
    env.setdefault("NODE_OPTIONS", "--max-old-space-size=4096")
    process = subprocess.Popen(
        command,
        cwd=epg_dir,
        env=env,
        start_new_session=True,
    )
    try:
        return process.wait(timeout=grab_timeout_sec)
    except subprocess.TimeoutExpired:
        print(f"grab timed out after {grab_timeout_sec}s; killing it", file=sys.stderr)
        os.killpg(process.pid, signal.SIGKILL)
        process.wait()
        return 124


def parse_guide(path: Path) -> tuple[dict[str, ET.Element], dict[str, list[ET.Element]]]:
    channels: dict[str, ET.Element] = {}
    programmes: dict[str, list[ET.Element]] = defaultdict(list)
    root = None
    for event, elem in ET.iterparse(path, events=("start", "end")):
        if event == "start" and root is None:
            root = elem
            continue
        if event != "end" or elem.tag not in {"channel", "programme"}:
            continue
        copied = ET.fromstring(ET.tostring(elem, encoding="utf-8"))
        if elem.tag == "channel":
            channel_id = copied.get("id") or ""
            if channel_id:
                channels[channel_id] = copied
        else:
            channel_id = copied.get("channel") or ""
            if channel_id:
                programmes[channel_id].append(copied)
        elem.clear()
        if root is not None:
            root.clear()
    return channels, programmes


def retitle(channel_elem: ET.Element, name: str) -> ET.Element:
    extras = [child for child in list(channel_elem) if child.tag != "display-name"]
    for child in list(channel_elem):
        channel_elem.remove(child)
    display = ET.Element("display-name")
    display.text = name
    channel_elem.append(display)
    for extra in extras:
        channel_elem.append(extra)
    return channel_elem


def synthetic_channel(tvg_id: str, name: str) -> ET.Element:
    channel = ET.Element("channel", {"id": tvg_id})
    display = ET.SubElement(channel, "display-name")
    display.text = name
    return channel


def write_gzip(path: Path, grabbed: list[GrabbedChannel]) -> None:
    tv = ET.Element("tv")
    tv.set("date", datetime.now(timezone.utc).strftime("%Y%m%d"))
    tv.set("generator-info-name", "iptv-org/epg")
    tv.set("source-info-name", "TurboSpeedAlmighty/EPG")
    grabbed = sorted(grabbed, key=lambda item: item.order)
    for item in grabbed:
        tv.append(item.channel_elem)
    for item in grabbed:
        item.programmes.sort(key=lambda programme: programme.get("start") or "")
        for programme in item.programmes:
            tv.append(programme)
    payload = b'<?xml version="1.0" encoding="UTF-8"?>\n' + ET.tostring(tv, encoding="utf-8")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".partial")
    with gzip.open(temporary, "wb", compresslevel=9) as handle:
        handle.write(payload)
    # Replace only after the gzip round-trips as XML.
    with gzip.open(temporary, "rb") as handle:
        ET.fromstring(handle.read())
    os.replace(temporary, path)


def summarize(grabbed: list[GrabbedChannel]) -> dict[str, int]:
    sites: dict[str, int] = defaultdict(int)
    for item in grabbed:
        sites[item.site] += 1
    return dict(sorted(sites.items(), key=lambda pair: (-pair[1], pair[0])))


def build(args: argparse.Namespace) -> int:
    lang = args.lang or PREFERRED_LANG.get(args.country, "")
    playlist_url = args.playlist or PLAYLISTS[args.country]
    print(f"downloading {playlist_url}", flush=True)
    playlist = parse_playlist(fetch_text(playlist_url))
    if args.only:
        wanted = {item.strip() for item in args.only.split(",") if item.strip()}
        playlist = [channel for channel in playlist if channel.tvg_id in wanted]
    if args.limit:
        playlist = playlist[: args.limit]
    print(f"{len(playlist)} playlist channels", flush=True)

    by_base = load_source_index(Path(args.epg_dir))
    candidates = select_candidates(playlist, by_base, lang)
    matched = sum(1 for channel in playlist if candidates[channel.tvg_id])
    exact = 0
    for channel in playlist:
        choices = candidates[channel.tvg_id]
        if choices and choices[0].xmltv_id == channel.tvg_id:
            exact += 1
    print(
        f"matched {matched}/{len(playlist)} channels "
        f"({exact} exact on the first site, {matched - exact} via another feed)",
        flush=True,
    )
    if args.select_only:
        histogram: dict[str, int] = defaultdict(int)
        for channel in playlist:
            choices = candidates[channel.tvg_id]
            if choices:
                histogram[choices[0].site] += 1
        print(json.dumps({"first_site": dict(sorted(histogram.items(), key=lambda p: -p[1]))}, indent=2))
        if args.explain:
            for tvg_id in args.explain:
                print(tvg_id)
                for index, source in enumerate(candidates.get(tvg_id, [])[:8], start=1):
                    kind = "exact" if source.xmltv_id == tvg_id else f"fallback:{source.xmltv_id}"
                    print(f"  {index}. {source.site} {kind} lang={source.lang} site_id={source.site_id}")
        return 0 if matched else 2

    grabbed_by_id: dict[str, GrabbedChannel] = {}
    next_index = {channel.tvg_id: 0 for channel in playlist}
    playlist_by_id = {channel.tvg_id: channel for channel in playlist}
    crashed_sites: set[str] = set()
    passes_run = 0

    with tempfile.TemporaryDirectory(prefix=f"epg-{args.country}-") as tmp:
        work = Path(tmp)
        for pass_number in range(1, args.max_passes + 1):
            batch: list[tuple[PlaylistChannel, SourceChannel]] = []
            for channel in playlist:
                if channel.tvg_id in grabbed_by_id:
                    continue
                choices = candidates[channel.tvg_id]
                index = next_index[channel.tvg_id]
                if index >= len(choices):
                    continue
                batch.append((channel, choices[index]))
                next_index[channel.tvg_id] = index + 1
            if not batch:
                break
            passes_run = pass_number
            by_site: dict[str, list[tuple[PlaylistChannel, SourceChannel]]] = defaultdict(list)
            for row in batch:
                by_site[row[1].site].append(row)
            print(
                f"pass {pass_number}: {len(batch)} channels across {len(by_site)} sites",
                flush=True,
            )
            for site, rows in sorted(by_site.items(), key=lambda item: site_rank(item[0])):
                channels_file = work / f"pass{pass_number}-{site}.channels.xml"
                output_file = work / f"pass{pass_number}-{site}.xml"
                write_channels_xml(channels_file, rows)
                print(f"pass {pass_number}: grabbing {len(rows)} channels from {site}", flush=True)
                code = run_grab(
                    Path(args.epg_dir),
                    channels_file,
                    output_file,
                    args.days,
                    args.max_connections,
                    args.timeout_ms,
                    args.grab_timeout,
                )
                parsed = None
                if code == 0 and output_file.is_file() and output_file.stat().st_size > 0:
                    try:
                        parsed = parse_guide(output_file)
                    except ET.ParseError as error:
                        print(f"{site} guide did not parse: {error}", file=sys.stderr)
                if parsed is None:
                    print(f"{site} grab failed (exit {code})", file=sys.stderr)
                    if site not in crashed_sites:
                        crashed_sites.add(site)
                        for playlist_channel, _source in rows:
                            next_index[playlist_channel.tvg_id] -= 1
                        print(f"will retry {site} once", flush=True)
                    continue
                channel_elems, programme_elems = parsed
                filled = 0
                for playlist_channel, source in rows:
                    programmes = programme_elems.get(playlist_channel.tvg_id, [])
                    if not programmes:
                        continue
                    channel_elem = channel_elems.get(playlist_channel.tvg_id)
                    if channel_elem is None:
                        channel_elem = synthetic_channel(playlist_channel.tvg_id, playlist_channel.name)
                    retitle(channel_elem, playlist_channel.name)
                    grabbed_by_id[playlist_channel.tvg_id] = GrabbedChannel(
                        tvg_id=playlist_channel.tvg_id,
                        name=playlist_channel.name,
                        order=playlist_channel.order,
                        site=source.site,
                        channel_elem=channel_elem,
                        programmes=programmes,
                    )
                    filled += 1
                print(f"{site}: {filled}/{len(rows)} channels with programmes", flush=True)

    grabbed = list(grabbed_by_id.values())
    programme_count = sum(len(item.programmes) for item in grabbed)
    summary = {
        "country": args.country,
        "playlist_channels": len(playlist),
        "matched_channels": matched,
        "channels_with_programmes": len(grabbed),
        "programmes": programme_count,
        "passes": passes_run,
        "sites": summarize(grabbed),
    }
    print(json.dumps(summary, indent=2), flush=True)
    output = Path(args.output)
    summary_path = output.with_suffix("").with_suffix(".summary.json")
    if output.name.endswith(".xml.gz"):
        summary_path = output.parent / f"{args.country}.summary.json"
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    summary_path.write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")

    if not grabbed:
        print(f"no channels with programmes for {args.country}; not writing {output}", flush=True)
        return 2

    write_gzip(output, grabbed)
    print(
        f"wrote {output} ({len(grabbed)} channels, {programme_count} programmes)",
        flush=True,
    )
    return 0


def self_test() -> int:
    lang = "en"
    entries = [
        SourceChannel("i.mjh.nz", "Flash/epg#cnn", "en", "CNN.us@SD", "CNN"),
        SourceChannel("tvpassport.com", "cnn/70", "en", "CNN.us@SD", "CNN"),
        SourceChannel("tvtv.us", "58646", "en", "CNN.us@SD", "CNN"),
        SourceChannel("tvguide.com", "kaal", "en", "ABC.us@KAAL", "ABC KAAL"),
        SourceChannel("i.mjh.nz", "PlutoTV/us#abc", "en", "ABC.us@HD", "ABC HD"),
        SourceChannel("plex.tv", "abc-west", "en", "ABC.us@West", "ABC West"),
        SourceChannel("tvpassport.com", "abc-east", "en", "ABC.us@East", "ABC East"),
        SourceChannel("gatotv.com", "azteca_uno", "es", "AztecaUno.mx@SD", "Azteca Uno"),
        SourceChannel("mi.tv", "mx#azteca-uno", "es", "AztecaUno.mx@SD", "Azteca Uno"),
        SourceChannel("i.mjh.nz", "other#cnnintl", "en", "CNNInternational.us@SD", "CNN Intl"),
    ]
    by_base: dict[str, list[SourceChannel]] = defaultdict(list)
    for entry in entries:
        by_base[base_id(entry.xmltv_id)].append(entry)

    def sites_for(tvg_id: str) -> list[str]:
        playlist = [PlaylistChannel(tvg_id, tvg_id, 0)]
        return [item.site for item in select_candidates(playlist, by_base, lang)[tvg_id]]

    cnn = sites_for("CNN.us@SD")
    assert cnn[0] == "i.mjh.nz", cnn
    assert "tvtv.us" not in cnn, cnn
    assert cnn.index("tvpassport.com") > cnn.index("i.mjh.nz")

    abc = select_candidates(
        [PlaylistChannel("ABC.us@KAAL", "ABC KAAL", 0)], by_base, lang
    )["ABC.us@KAAL"]
    assert [item.site for item in abc] == ["tvguide.com", "i.mjh.nz", "tvpassport.com", "plex.tv"], [
        item.site for item in abc
    ]
    assert abc[0].xmltv_id == "ABC.us@KAAL"
    assert abc[1].xmltv_id == "ABC.us@HD"

    azteca = sites_for("AztecaUno.mx@SD")
    assert azteca[:2] == ["gatotv.com", "mi.tv"], azteca

    playlist = parse_playlist(
        '#EXTM3U\n'
        '#EXTINF:-1 tvg-id="CNN.us@SD" tvg-logo="https://example.com/a,b.png" group-title="News",CNN\n'
        "http://example\n"
        '#EXTINF:-1 tvg-id="CNN.us@SD" group-title="News",CNN duplicate\n'
        "http://example\n"
        "#EXTINF:-1 group-title=\"News\",No id\n"
        "http://example\n"
    )
    assert [channel.tvg_id for channel in playlist] == ["CNN.us@SD"]
    assert playlist[0].name == "CNN"

    print("self-test ok")
    return 0


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build an XMLTV guide for one country playlist")
    parser.add_argument("--country", choices=sorted(PLAYLISTS), help="us or mx")
    parser.add_argument("--playlist", help="playlist URL (defaults to the iptv-org country file)")
    parser.add_argument("--lang", help="preferred guide language (defaults from the country)")
    parser.add_argument("--epg-dir", default=os.environ.get("EPG_DIR", "epg"), help="iptv-org/epg checkout")
    parser.add_argument("--output", help="destination .xml.gz")
    parser.add_argument("--days", type=int, default=3)
    parser.add_argument("--max-connections", type=int, default=10)
    parser.add_argument("--max-passes", type=int, default=4)
    parser.add_argument("--timeout-ms", type=int, default=30000)
    parser.add_argument("--grab-timeout", type=int, default=90 * 60, help="seconds before a site grab is killed")
    parser.add_argument("--limit", type=int, default=0, help="only the first N playlist channels (debug)")
    parser.add_argument("--only", default="", help="comma-separated tvg-ids to build (debug)")
    parser.add_argument("--select-only", action="store_true", help="map channels and exit without grabbing")
    parser.add_argument("--explain", nargs="*", default=[], help="with --select-only, print candidates for these ids")
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args(argv)
    if args.self_test:
        return args
    if not args.country or not args.output:
        parser.error("--country and --output are required")
    return args


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    if args.self_test:
        return self_test()
    try:
        return build(args)
    except Exception as error:  # noqa: BLE001 - one country must not skip the other country's build
        traceback.print_exc()
        print(f"build failed: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
