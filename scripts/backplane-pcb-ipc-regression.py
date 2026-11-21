#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Exercise the headless PCB operations added to the stable fork."""

import argparse
from pathlib import Path
import uuid
import re

from google.protobuf.empty_pb2 import Empty

from backplane_ipc_test_support import IpcSession


def check_first_net_queries(api):
    project = api.module("common.commands.project_commands")
    editor = api.module("common.commands.editor_commands")
    commands = api.module("board.board_commands")
    board = api.module("board.board_types")
    types = api.module("common.types.base_types")
    enums = api.module("common.types.enums")
    track_id, via_id, free_via_id, pad_id, zone_id, bottom_zone_id = (str(uuid.uuid4()) for _ in range(6))
    second_plane_via = str(uuid.uuid4())
    polygon = "(pts (xy 18 18) (xy 22 18) (xy 22 22) (xy 18 22))"
    clearance = "(pts (xy 18 18) (xy 22 18) (xy 22 19) (xy 19 19) (xy 19 22) (xy 18 22))"
    rim = "(pts (xy 18 18) (xy 19.8 18) (xy 19.8 22) (xy 18 22))"

    # Track net, via net, bottom-layer fill and the expected via net after propagation.
    cases = (("assigned", 1, 2, "", "PLANE"),
             ("pad-propagation", 0, 2, "", "PLANE"),
             ("zone-propagation", 1, 1, "", "PLANE"),
             ("single-plane-via", 1, 2, polygon, "PLANE"),
             ("overlapping-planes", 1, 2, polygon, "PLANE"),
             ("plane-clearance", 1, 1, clearance, "PLANE"),
             ("rim-contact", 1, 1, rim, "PAD_NET"),
             ("stale-fill", 1, 1, polygon, "PLANE"))
    for name, track_net, via_net, bottom_polygon, expected_via_net in cases:
        path = api.root / (name + ".kicad_pcb")
        path.with_suffix(".kicad_pro").write_text("{}", encoding="utf-8")
        bottom_fill = f'(fill yes) (filled_polygon (layer "B.Cu") {bottom_polygon})' if bottom_polygon else ""
        bottom_outline = "(pts (xy 28 28) (xy 32 28) (xy 32 32) (xy 28 32))" if name == "stale-fill" else polygon
        extra_via = (f'(via (at 19 20) (size 0.8) (drill 0.4) (layers "F.Cu" "B.Cu") '
                     f'(net 2) (uuid "{second_plane_via}"))') if name == "overlapping-planes" else ""
        path.write_text(f'''(kicad_pcb (version 20241229) (generator "pcbnew")
          (general (thickness 1.6)) (paper "A4")
          (layers (0 "F.Cu" signal) (2 "B.Cu" signal)) (setup (pad_to_mask_clearance 0))
          (net 0 "") (net 1 "PAD_NET") (net 2 "PLANE")
          (footprint "Test:Pad" (layer "F.Cu") (at 10 10)
            (pad "1" smd rect (at 0 0) (size 2 2) (layers "F.Cu") (net 1 "PAD_NET") (uuid "{pad_id}")))
          (segment (start 10 10) (end 14 10) (width 0.3) (layer "F.Cu") (net {track_net}) (uuid "{track_id}"))
          (via (at 20 20) (size 0.8) (drill 0.4) (layers "F.Cu" "B.Cu") (net {via_net}) (uuid "{via_id}"))
          (via (at 21 20) (size 0.8) (drill 0.4) (layers "F.Cu" "B.Cu") (free yes) (net 1) (uuid "{free_via_id}"))
          {extra_via}
          (zone (net 2) (net_name "PLANE") (layer "F.Cu") (uuid "{zone_id}") (hatch edge 0.5)
            (connect_pads (clearance 0.2)) (min_thickness 0.25) (fill yes)
            (polygon {polygon}) (filled_polygon (layer "F.Cu") {polygon}))
          (zone (net 1) (net_name "PAD_NET") (layer "B.Cu") (uuid "{bottom_zone_id}") (hatch edge 0.5)
            (connect_pads (clearance 0.2)) (min_thickness 0.25) (polygon {bottom_outline}) {bottom_fill}))''', encoding="utf-8")
        doc = api.request(project.OpenDocument(type=types.DOCTYPE_PCB, path=str(path)),
                          project.OpenDocumentResponse).document
        header = types.ItemHeader(document=doc)

        def members(command):
            response = api.request(command, editor.GetItemsResponse)
            assert response.status == types.IRS_OK
            result = {}
            for packed in response.items:
                for cls in (board.Track, board.Via, board.Pad, board.Zone):
                    item = cls()
                    if packed.Unpack(item):
                        assert item.id.value not in result
                        result[item.id.value] = (item.copper_settings.net if cls is board.Zone else item.net).name
                        break
                else:
                    raise AssertionError(packed.type_url)
            return result

        by_net = commands.GetItemsByNet(header=header, nets=[board.Net(name="PAD_NET"), board.Net(name="PLANE")])
        by_class = commands.GetItemsByNetClass(header=header)
        expected = {track_id: "PAD_NET", pad_id: "PAD_NET", free_via_id: "PAD_NET",
                    via_id: expected_via_net, zone_id: "PLANE"}
        if bottom_fill:
            expected[bottom_zone_id] = "PAD_NET"
        if extra_via:
            expected[second_plane_via] = "PLANE"
        cold = members(by_net if name == "pad-propagation" else by_class)
        assert cold == expected, (name, cold.items() - expected.items(), expected.items() - cold.items())
        assert members(by_net) == expected, name

        if name == "assigned":
            added = board.Track(id=types.KIID(value=str(uuid.uuid4())), layer=board.BL_F_Cu,
                                net=board.Net(name="PAD_NET"), width=types.Distance(value_nm=300000),
                                start=types.Vector2(x_nm=30000000, y_nm=30000000),
                                end=types.Vector2(x_nm=34000000, y_nm=30000000))
            create = editor.CreateItems(header=header)
            create.items.add().Pack(added)
            created = api.request(create, editor.CreateItemsResponse).created_items[0]
            assert created.status.code == editor.ISC_OK and created.item.Unpack(added)
            assert members(by_net) == {**expected, added.id.value: "PAD_NET"}
            api.request(editor.DeleteItems(header=header, item_ids=[added.id]), editor.DeleteItemsResponse)
            assert members(by_net) == expected

        connected = members(commands.GetConnectedItems(header=header, items=[types.KIID(value=track_id)],
                                                       types=[enums.KOT_PCB_PAD]))
        assert connected == {pad_id: "PAD_NET"}, name
        assert members(by_class) == members(by_net) == expected, name
        api.request(project.CloseAllDocuments(force=True), Empty)
    print("PASS first PCB net queries, pad and zone propagation, free vias, physical connections and edits", flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("kicad_cli", type=Path)
    args = parser.parse_args()
    with IpcSession(args.kicad_cli) as api:
        check_first_net_queries(api)
        project = api.module("common.commands.project_commands")
        editor = api.module("common.commands.editor_commands")
        commands = api.module("board.board_commands")
        board = api.module("board.board_types")
        rules = api.module("board.board")
        types = api.module("common.types.base_types")
        jobs = api.module("common.types.jobs")
        enums = api.module("common.types.enums")
        sch_jobs = api.module("schematic.schematic_jobs")
        design = api.copy_project("demos/ecc83", "pcb-regression")
        board_path = design / "ecc83-pp.kicad_pcb"
        schematic_path = design / "ecc83-pp.kicad_sch"
        document = api.request(project.OpenDocument(type=types.DOCTYPE_PCB, path=str(board_path)),
                               project.OpenDocumentResponse).document
        header = types.ItemHeader(document=document)

        def request(command, response=Empty, **fields):
            return api.request(command(board=document, **fields), response)

        def items(kind):
            result = api.request(editor.GetItems(header=header, types=[kind]), editor.GetItemsResponse)
            assert result.status == types.IRS_OK, result
            return result.items

        def ids_of_tracks(packed_tracks):
            ids = set()
            for packed in packed_tracks:
                track = board.Track()
                assert packed.Unpack(track)
                ids.add(track.id.value)
            return ids

        initial_tracks = ids_of_tracks(items(enums.KOT_PCB_TRACE))
        by_class = api.request(commands.GetItemsByNetClass(header=header, types=[enums.KOT_PCB_TRACE]),
                               editor.GetItemsResponse)
        assert initial_tracks and ids_of_tracks(by_class.items) == initial_tracks

        layers = request(commands.GetBoardEnabledLayers, commands.BoardEnabledLayersResponse)
        changed_layers = request(commands.SetBoardEnabledLayers, commands.BoardEnabledLayersResponse,
                                 copper_layer_count=layers.copper_layer_count, layers=layers.layers)
        assert changed_layers == layers
        for invalid_count in (0, 1, 3, 1024):
            rejected = api.request_raw(commands.SetBoardEnabledLayers(
                board=document, copper_layer_count=invalid_count, layers=layers.layers))
            assert rejected.status.status == api.envelope.AS_BAD_REQUEST, rejected
            assert request(commands.GetBoardEnabledLayers, commands.BoardEnabledLayersResponse) == layers
        for origin_type in (commands.BOT_GRID, commands.BOT_DRILL):
            original = request(commands.GetBoardOrigin, types.Vector2, type=origin_type)
            origin = types.Vector2(x_nm=11000000, y_nm=12000000)
            request(commands.SetBoardOrigin, type=origin_type, origin=origin)
            assert request(commands.GetBoardOrigin, types.Vector2, type=origin_type) == origin
            request(commands.SetBoardOrigin, type=origin_type, origin=original)
        zones = []
        for packed in items(enums.KOT_PCB_ZONE):
            zone = board.Zone()
            assert packed.Unpack(zone)
            if zone.type != board.ZT_RULE_AREA:
                zones.append(zone.id)
        assert zones, "Fixture requires a copper zone"
        request(commands.RefillZones, zones=zones[:1])
        request(commands.RefillZones)
        print("PASS PCB enabled layers, both origins, selected/all zone refill", flush=True)

        traces = items(enums.KOT_PCB_TRACE)
        sample = board.Track()
        assert traces[0].Unpack(sample)
        tracks = []
        for offset in (0, 1000000):
            track = board.Track(id=types.KIID(value=str(uuid.uuid4())),
                                start=types.Vector2(x_nm=500000000 + offset, y_nm=500000000),
                                end=types.Vector2(x_nm=501000000 + offset, y_nm=500000000),
                                width=types.Distance(value_nm=250000), layer=board.BL_F_Cu,
                                net=sample.net)
            tracks.append(track)
        transaction = api.request(editor.BeginCommit(header=header), editor.BeginCommitResponse)
        create = editor.CreateItems(header=header)
        for track in tracks:
            create.items.add().Pack(track)
        created = api.request(create, editor.CreateItemsResponse)
        assert all(item.status.code == editor.ISC_OK for item in created.created_items), created
        api.request(editor.EndCommit(header=header, id=transaction.id, action=editor.CMA_COMMIT,
                                     message="Connectivity regression"), editor.EndCommitResponse)

        def track_ids(command):
            result = api.request(command, editor.GetItemsResponse)
            assert result.status == types.IRS_OK, result
            return ids_of_tracks(result.items)

        connected = track_ids(commands.GetConnectedItems(header=header, items=[tracks[0].id],
                                                        types=[enums.KOT_PCB_TRACE]))
        assert tracks[1].id.value in connected, connected
        by_net = track_ids(commands.GetItemsByNet(header=header, nets=[sample.net],
                                                 types=[enums.KOT_PCB_TRACE]))
        assert {track.id.value for track in tracks} <= by_net
        second = board.Track()
        assert any(packed.Unpack(second) and second.net.name != sample.net.name for packed in traces)

        def tracks_for(nets):
            return api.request(commands.GetItemsByNet(header=header, nets=nets,
                               types=[enums.KOT_PCB_TRACE]), editor.GetItemsResponse).items

        two_nets = list(tracks_for([second.net])) + list(tracks_for([sample.net]))
        missing_net = board.Net(name="Backplane fixture absent net")
        assert list(tracks_for([second.net, missing_net, sample.net, second.net])) == two_nets
        by_class = track_ids(commands.GetItemsByNetClass(header=header, net_classes=["Default"],
                                                       types=[enums.KOT_PCB_TRACE]))
        assert by_class, "Default netclass query returned no tracks"
        api.request(editor.AddToSelection(header=header, items=[track.id for track in tracks]),
                    editor.SelectionResponse)
        assert len(api.request(editor.GetSelection(header=header), editor.SelectionResponse).items) == 2
        api.request(editor.ClearSelection(header=header), Empty)
        assert not api.request(editor.GetSelection(header=header), editor.SelectionResponse).items
        invalid_types = api.request_raw(commands.GetConnectedItems(
            header=header, items=[tracks[0].id], types=[enums.KOT_SCH_SYMBOL]))
        assert invalid_types.status.status == api.envelope.AS_BAD_REQUEST, invalid_types
        print("PASS PCB connectivity, net/netclass queries, headless selection", flush=True)

        design_rules = request(commands.GetBoardDesignRules, commands.BoardDesignRulesResponse).rules
        design_rules.constraints.min_clearance.value_nm = 230000
        assert len(design_rules.teardrops.target_params) == 3
        design_rules.teardrops.target_vias = not design_rules.teardrops.target_vias
        design_rules.teardrops.target_params[0].params.max_length.value_nm = 1700000
        request(commands.SetBoardDesignRules, commands.BoardDesignRulesResponse, rules=design_rules)
        assert request(commands.GetBoardDesignRules, commands.BoardDesignRulesResponse).rules.constraints.min_clearance.value_nm == 230000
        assert request(commands.GetBoardDesignRules, commands.BoardDesignRulesResponse).rules.teardrops == design_rules.teardrops
        before_invalid = request(commands.GetBoardDesignRules, commands.BoardDesignRulesResponse).rules
        invalid = rules.BoardDesignRules()
        invalid.CopyFrom(before_invalid)
        invalid.constraints.min_clearance.value_nm = 999999
        invalid.severities.add(error_key="backplane-invalid-key", severity=types.RS_ERROR)
        rejected = api.request_raw(commands.SetBoardDesignRules(board=document, rules=invalid))
        assert rejected.status.status == api.envelope.AS_BAD_REQUEST, rejected
        assert request(commands.GetBoardDesignRules, commands.BoardDesignRulesResponse).rules == before_invalid
        custom = rules.CustomRule(name="Backplane regression clearance", condition="A.Type == 'Track'",
                                  severity=types.RS_ERROR)
        custom.constraints.add(type=rules.CRCT_CLEARANCE, numeric=types.MinOptMax(min=240000))
        request(commands.SetCustomDesignRules, commands.CustomRulesResponse, rules=[custom])
        restored = request(commands.GetCustomDesignRules, commands.CustomRulesResponse)
        assert restored.status == commands.CRS_VALID and restored.rules[0].name == custom.name, restored
        assert restored.rules[0].constraints[0].numeric.min == 240000, restored
        plot = request(commands.GetBoardPlotSettings, commands.BoardPlotSettingsResponse).plot_settings
        plot.mirror = not plot.mirror
        request(commands.SetBoardPlotSettings, plot_settings=plot)
        assert request(commands.GetBoardPlotSettings, commands.BoardPlotSettingsResponse).plot_settings.mirror == plot.mirror
        print("PASS board/custom rules and plot-settings read/write", flush=True)

        schematic = api.request(project.OpenDocument(type=types.DOCTYPE_SCHEMATIC,
                                                      path=str(schematic_path)),
                                project.OpenDocumentResponse).document
        netlist_path = design / "updated-netlist.net"
        result = api.request(sch_jobs.RunSchematicJobExportNetlist(
            job_settings=jobs.RunJobSettings(document=schematic, output_path=str(netlist_path)),
            format=sch_jobs.SNF_KICAD_SEXPR), jobs.RunJobResponse)
        assert result.status == jobs.JS_SUCCESS, result
        native_netlist = netlist_path.read_text()
        component = re.search(r'(\(comp\s+\(ref "(R[^"\n]+)"\)\s+\(value ")([^"\n]*)("\))',
                              native_netlist)
        assert component, "Fixture has no resistor in its native netlist"
        reference = component.group(2)
        original_value = component.group(3)
        native_netlist = (native_netlist[:component.start(3)] + "2.2k IPC" +
                          native_netlist[component.end(3):])
        netlist_path.write_text(native_netlist)

        def value():
            for packed in items(enums.KOT_PCB_FOOTPRINT):
                footprint = board.FootprintInstance()
                assert packed.Unpack(footprint)
                if footprint.reference_field.text.text.text == reference:
                    return footprint.value_field.text.text.text
            raise AssertionError(f"Footprint {reference} is missing")

        before = value()
        result = request(commands.ImportNetlist, commands.ImportNetlistResponse,
                         netlist_path=str(netlist_path), dry_run=True, match_mode=commands.NMM_REFERENCE)
        assert result.error_count == 0, result.report
        assert value() == before, "Dry-run netlist import mutated the board"
        result = request(commands.ImportNetlist, commands.ImportNetlistResponse,
                         netlist_path=str(netlist_path), match_mode=commands.NMM_REFERENCE)
        assert result.error_count == 0, result.report
        assert value() == "2.2k IPC", (original_value, value())
        api.request(project.SaveDocument(document=document), Empty)
        api.request(project.CloseDocument(document=document), Empty)
        document = api.request(project.OpenDocument(type=types.DOCTYPE_PCB, path=str(board_path)),
                               project.OpenDocumentResponse).document
        header = types.ItemHeader(document=document)
        assert value() == "2.2k IPC"
        assert request(commands.GetBoardDesignRules, commands.BoardDesignRulesResponse).rules.constraints.min_clearance.value_nm == 230000
        assert request(commands.GetBoardPlotSettings, commands.BoardPlotSettingsResponse).plot_settings.mirror == plot.mirror
        assert request(commands.GetBoardDesignRules, commands.BoardDesignRulesResponse).rules.teardrops == design_rules.teardrops
        print("PASS netlist dry-run/update and save/reopen persistence", flush=True)
        api.request(project.CloseAllDocuments(force=True), Empty)


if __name__ == "__main__":
    main()
