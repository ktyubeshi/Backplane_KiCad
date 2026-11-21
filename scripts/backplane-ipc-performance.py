#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Measure Linux headless IPC round trips on a disposable copy of the video demo.

Uses protoc, protobuf and pynng as in backplane-ipc-smoke.py. Each open sample
closes the previous document and reuses the OS file cache.
"""

import argparse
import hashlib
from pathlib import Path
from statistics import median
from time import perf_counter_ns

from google.protobuf.empty_pb2 import Empty

from backplane_ipc_test_support import IpcSession


def measure_first_net(api, path, samples):
    project = api.module("common.commands.project_commands")
    editor = api.module("common.commands.editor_commands")
    types = api.module("common.types.base_types")
    enums = api.module("common.types.enums")
    board = api.module("board.board_commands")
    board_types = api.module("board.board_types")

    def memberships(response):
        result = {}
        for packed in response.items:
            via = board_types.Via()
            assert packed.Unpack(via)
            assert via.id.value not in result, "Duplicate via in response"
            result[via.id.value] = via.net.name
        return result

    timings = []
    expected = None
    for sample in range(samples + 1):
        api.request(project.CloseAllDocuments(force=True), Empty)
        opening = project.OpenDocument(type=types.DOCTYPE_PCB, path=str(path))
        start = perf_counter_ns()
        doc = api.request(opening, project.OpenDocumentResponse).document
        opened = perf_counter_ns()
        header = types.ItemHeader(document=doc)
        nets = api.request(board.GetNets(board=doc), board.NetsResponse).nets
        listed = perf_counter_ns()
        result = api.request(board.GetItemsByNet(header=header, nets=nets, types=[enums.KOT_PCB_VIA]),
                             editor.GetItemsResponse)
        finished = perf_counter_ns()

        # Check each fresh result after stopping the clock. No zone refill or prior item query.
        actual = memberships(result)
        if expected is None:
            expected = memberships(api.request(editor.GetItems(header=header, types=[enums.KOT_PCB_VIA]),
                                               editor.GetItemsResponse))
        assert nets and expected and actual == expected, "First net query returned different via assignments"
        if sample:
            timings.append((opened - start, listed - opened, finished - listed, finished - start))

    labels = ("OpenDocument PCB", "GetNets", "First GetItemsByNet (vias)", "OpenDocument to first net result")
    for label, times in zip(labels, zip(*timings)):
        print(f"{label:<40} {median(times) / 1e6:9.2f} ms (median, n={samples})")
    payload = "\n".join(f"{key}\t{expected[key]}" for key in sorted(expected)).encode()
    print(f"Verified {len(nets)} nets, {len(expected)} vias, assignment SHA-256 {hashlib.sha256(payload).hexdigest()}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("kicad_cli", type=Path)
    parser.add_argument("--samples", type=int, default=9)
    parser.add_argument("--first-net", action="store_true",
                        help="Time OpenDocument, net discovery and the first via query on each freshly opened board")
    args = parser.parse_args()
    if args.samples < 1:
        parser.error("--samples must be positive")

    with IpcSession(args.kicad_cli) as api:
        editor = api.module("common.commands.editor_commands")
        project = api.module("common.commands.project_commands")
        types = api.module("common.types.base_types")
        enums = api.module("common.types.enums")
        schematic = api.module("schematic.schematic_types")
        board = api.module("board.board_commands")
        board_types = api.module("board.board_types")
        demo = api.copy_project("demos/video", "benchmark")

        if args.first_net:
            measure_first_net(api, demo / "video.kicad_pcb", args.samples)
            return

        def measure(label, command, response_type, before=None):
            times = []
            for sample in range(args.samples + 1):
                if before:
                    before()
                start = perf_counter_ns()
                response = api.request(command, response_type)
                elapsed = (perf_counter_ns() - start) / 1e6
                if sample:
                    times.append(elapsed)
            print(f"{label:<40} {median(times):9.2f} ms (median, n={len(times)})", flush=True)
            return response

        def close():
            api.request(project.CloseAllDocuments(force=True), Empty)

        def open_document(kind, extension):
            return measure(f"OpenDocument {extension}",
                           project.OpenDocument(type=kind, path=str(demo / f"video.{extension}")),
                           project.OpenDocumentResponse, before=close).document

        doc = open_document(types.DOCTYPE_SCHEMATIC, "kicad_sch")
        header = types.ItemHeader(document=doc)
        symbols = measure("GetItems schematic symbols",
                          editor.GetItems(header=header, types=[enums.KOT_SCH_SYMBOL]),
                          editor.GetItemsResponse)
        assert symbols.items
        lines = measure("GetItems schematic lines",
                        editor.GetItems(header=header, types=[enums.KOT_SCH_LINE]),
                        editor.GetItemsResponse)

        def line_ids(response):
            result = []
            for packed in response.items:
                line = schematic.SchematicLine()
                assert packed.Unpack(line)
                result.append(line.id.value)
            return result

        ordered_ids = sorted(set(line_ids(lines)))[:512]
        assert ordered_ids
        single = measure("GetItemsById (one schematic line)",
                         editor.GetItemsById(header=header, items=[types.KIID(value=ordered_ids[0])]),
                         editor.GetItemsResponse)
        assert line_ids(single) == ordered_ids[:1]
        lookup = editor.GetItemsById(header=header, items=[types.KIID(value=i) for i in ordered_ids])
        found = measure(f"GetItemsById ({len(ordered_ids)} schematic lines)", lookup, editor.GetItemsResponse)
        assert line_ids(found) == ordered_ids

        doc = open_document(types.DOCTYPE_PCB, "kicad_pcb")
        header = types.ItemHeader(document=doc)
        # Build connectivity outside the measured requests on older headless runtimes, too.
        zones = api.request(editor.GetItems(header=header, types=[enums.KOT_PCB_ZONE]),
                            editor.GetItemsResponse)
        for packed in zones.items:
            zone = board_types.Zone()
            assert packed.Unpack(zone)
            if zone.type != board_types.ZT_RULE_AREA:
                api.request(board.RefillZones(board=doc, zones=[zone.id]), Empty)
                break
        else:
            raise AssertionError("The benchmark board needs a copper zone")
        all_vias = api.request(editor.GetItems(header=header, types=[enums.KOT_PCB_VIA]),
                               editor.GetItemsResponse)
        nets = api.request(board.GetNets(board=doc), board.NetsResponse).nets
        assert nets
        vias = measure(f"GetItemsByNet ({len(nets)} nets, vias)",
                       board.GetItemsByNet(header=header, nets=nets, types=[enums.KOT_PCB_VIA]),
                       editor.GetItemsResponse)
        by_class = measure("GetItemsByNetClass (all classes, vias)",
                           board.GetItemsByNetClass(header=header, types=[enums.KOT_PCB_VIA]),
                           editor.GetItemsResponse)

        def via_ids(response):
            ids = set()
            for packed in response.items:
                via = board_types.Via()
                assert packed.Unpack(via)
                ids.add(via.id.value)
            assert len(ids) == len(response.items), "Duplicate via IDs in response"
            return ids

        expected = via_ids(all_vias)
        assert expected, "The benchmark board needs vias"
        for result in (vias, by_class):
            actual = via_ids(result)
            assert actual == expected, f"Expected {len(expected)} vias, found {len(actual)}"
        print(f"Verified {len(expected)} vias", flush=True)


if __name__ == "__main__":
    main()
