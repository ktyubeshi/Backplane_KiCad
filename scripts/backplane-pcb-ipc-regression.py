#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Exercise the headless PCB operations added to the stable fork."""

import argparse
from pathlib import Path
import subprocess
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
        rules = api.module("board.board_rules")
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

        model_type_url = "type.googleapis.com/kiapi.board.types.Footprint3DModel"
        pad_type_url = "type.googleapis.com/kiapi.board.types.Pad"

        def footprint_state():
            state = {}
            for packed in items(enums.KOT_PCB_FOOTPRINT):
                footprint = board.FootprintInstance()
                assert packed.Unpack(footprint)
                state[footprint.id.value] = (
                    footprint.position.x_nm,
                    footprint.position.y_nm,
                    footprint.layer,
                    sum(item.type_url == model_type_url for item in footprint.definition.items),
                    sum(item.type_url == pad_type_url for item in footprint.definition.items),
                    round(footprint.orientation.value_degrees, 6),
                    footprint.locked,
                )
            return state

        # Give one footprint a non-default rotation and lock with a full update first, so the
        # partial update below must keep them.
        packed_first = items(enums.KOT_PCB_FOOTPRINT)[0]
        first = board.FootprintInstance()
        assert packed_first.Unpack(first)
        first.orientation.value_degrees = 90.0
        first.locked = types.LS_LOCKED
        transaction = api.request(editor.BeginCommit(header=header), editor.BeginCommitResponse)
        full = editor.UpdateItems(header=header)
        full.items.add().Pack(first)
        assert api.request(full, editor.UpdateItemsResponse).updated_items[0].status.code == editor.ISC_OK
        api.request(editor.EndCommit(header=header, id=transaction.id, action=editor.CMA_COMMIT,
                                     message="Rotate and lock before partial update"), editor.EndCommitResponse)

        before_footprints = footprint_state()
        assert before_footprints[first.id.value][5] == 90.0, before_footprints[first.id.value]
        assert before_footprints[first.id.value][6] == types.LS_LOCKED, before_footprints[first.id.value]
        original_footprint_count = len(before_footprints)
        original_model_count = sum(state[3] for state in before_footprints.values())
        original_pad_count = sum(state[4] for state in before_footprints.values())
        original_model_entries = board_path.read_text(encoding="utf-8").count("(model ")

        partial_updates = []
        for footprint_id, state in before_footprints.items():
            partial = board.FootprintInstance()
            partial.id.value = footprint_id
            partial.position.x_nm = state[0] + 1000000
            partial.position.y_nm = state[1]
            partial_updates.append(partial)

        transaction = api.request(editor.BeginCommit(header=header), editor.BeginCommitResponse)
        update = editor.UpdateItems(header=header)
        for partial in partial_updates:
            update.items.add().Pack(partial)
        update_result = api.request(update, editor.UpdateItemsResponse)
        assert update_result.status == types.IRS_OK, update_result
        assert len(update_result.updated_items) == len(partial_updates), update_result
        assert all(item.status.code == editor.ISC_OK for item in update_result.updated_items), update_result
        api.request(editor.EndCommit(header=header, id=transaction.id, action=editor.CMA_COMMIT,
                                     message="Partial footprint update regression"), editor.EndCommitResponse)

        after_footprints = footprint_state()
        assert set(after_footprints) == set(before_footprints)
        for footprint_id, state in before_footprints.items():
            updated = after_footprints[footprint_id]
            assert updated[0] == state[0] + 1000000
            assert updated[1] == state[1]
            assert updated[2] == state[2], "partial update changed the layer"
            assert updated[3] == state[3]
            assert updated[4] == state[4]
            assert updated[5] == state[5], "partial update changed the rotation"
            assert updated[6] == state[6], "partial update changed the lock"
        assert sum(state[3] for state in after_footprints.values()) == original_model_count
        assert sum(state[4] for state in after_footprints.values()) == original_pad_count

        api.request(project.SaveDocument(document=document), Empty)
        assert board_path.read_text(encoding="utf-8").count("(model ") == original_model_entries
        api.request(project.CloseDocument(document=document), Empty)
        document = api.request(project.OpenDocument(type=types.DOCTYPE_PCB, path=str(board_path)),
                               project.OpenDocumentResponse).document
        header = types.ItemHeader(document=document)
        reopened_footprints = footprint_state()
        for footprint_id, state in before_footprints.items():
            updated = reopened_footprints[footprint_id]
            assert updated[0] == state[0] + 1000000
            assert updated[1] == state[1]
            assert updated[2] == state[2], "partial update changed the layer"
            assert updated[3] == state[3]
            assert updated[4] == state[4]
            assert updated[5] == state[5], "partial update changed the rotation"
            assert updated[6] == state[6], "partial update changed the lock"
        assert sum(state[3] for state in reopened_footprints.values()) == original_model_count
        assert sum(state[4] for state in reopened_footprints.values()) == original_pad_count
        print("PASS partial footprint updates keep pads, models, layer, rotation and lock "
              "in memory and after save/reopen", flush=True)

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

        sample = board.Track()
        assert items(enums.KOT_PCB_TRACE)[0].Unpack(sample)
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
            result_ids = set()
            for packed in result.items:
                track = board.Track()
                if packed.Unpack(track):
                    result_ids.add(track.id.value)
            return result_ids

        connected = track_ids(commands.GetConnectedItems(header=header, items=[tracks[0].id],
                                                        types=[enums.KOT_PCB_TRACE]))
        assert tracks[1].id.value in connected, connected
        by_net = track_ids(commands.GetItemsByNet(header=header, nets=[sample.net],
                                                 types=[enums.KOT_PCB_TRACE]))
        assert {track.id.value for track in tracks} <= by_net
        by_class = track_ids(commands.GetItemsByNetClass(header=header, net_classes=["Default"],
                                                       types=[enums.KOT_PCB_TRACE]))
        assert by_class, "Default netclass query returned no tracks"
        api.expect_headless_unavailable(
            editor.AddToSelection(header=header, items=[track.id for track in tracks]),
            editor.GetSelection(header=header),
            editor.ClearSelection(header=header),
        )
        invalid_types = api.request_raw(commands.GetConnectedItems(
            header=header, items=[tracks[0].id], types=[enums.KOT_SCH_SYMBOL]))
        # The 10.0.6 runtime rejected a schematic type filter; upstream filters it to nothing.
        if invalid_types.status.status == api.envelope.AS_OK:
            filtered = editor.GetItemsResponse()
            assert invalid_types.message.Unpack(filtered), invalid_types
            assert not filtered.items, filtered
        else:
            assert invalid_types.status.status == api.envelope.AS_BAD_REQUEST, invalid_types
        print("PASS PCB connectivity, net/netclass queries; headless selection reported unavailable", flush=True)

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
        # Upstream keys severities by DrcErrorType and rejects anything but error/warning/ignore.
        invalid.severities.add(rule_type=rules.DRCET_CLEARANCE, severity=types.RS_INFO)
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

        fresh_board_path = design / "fresh.kicad_pcb"
        api.request(project.CreateDocument(type=types.DOCTYPE_PCB, path=str(fresh_board_path)),
                    project.OpenDocumentResponse)
        opened_boards = api.request(editor.GetOpenDocuments(type=types.DOCTYPE_PCB),
                                    editor.GetOpenDocumentsResponse)
        assert len(opened_boards.documents) == 1, opened_boards
        fresh_document = opened_boards.documents[0]
        assert fresh_document.board_filename == fresh_board_path.name, fresh_document

        fresh_netlist_path = design / "fresh-netlist.net"
        netlist_export = subprocess.run(
            [str(api.cli), "sch", "export", "netlist", "--format", "kicadsexpr",
             "-o", str(fresh_netlist_path), str(schematic_path)],
            env=api.env, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            timeout=120,
        )
        assert netlist_export.returncode == 0, netlist_export.stdout

        fresh_import = api.request(
            commands.ImportNetlist(board=fresh_document, netlist_path=str(fresh_netlist_path),
                                   match_mode=commands.NMM_REFERENCE),
            commands.ImportNetlistResponse,
        )
        assert fresh_import.error_count == 0, fresh_import.report
        expected_new_footprints = 15 if original_footprint_count == 15 else original_footprint_count
        expected_models = 11 if original_model_count == 11 else original_model_count
        assert fresh_import.new_footprint_count == expected_new_footprints, fresh_import

        fresh_header = types.ItemHeader(document=fresh_document)
        fresh_items = api.request(
            editor.GetItems(header=fresh_header, types=[enums.KOT_PCB_FOOTPRINT]),
            editor.GetItemsResponse,
        )
        assert fresh_items.status == types.IRS_OK, fresh_items
        model_paths = []
        kicad10_model_prefix = "${KICAD10_3DMODEL_DIR}/"
        legacy_model_prefix = "${KICAD6_3DMODEL_DIR}/"
        project_model_prefix = "${KIPRJMOD}/"
        for packed in fresh_items.items:
            footprint = board.FootprintInstance()
            assert packed.Unpack(footprint)
            for item in footprint.definition.items:
                if item.type_url != model_type_url:
                    continue
                model = board.Footprint3DModel()
                assert item.Unpack(model)
                # The fixture includes official-library, legacy-library, and project-local
                # model paths; all are valid and must survive the import unchanged.
                assert model.filename.startswith(
                    (kicad10_model_prefix, legacy_model_prefix, project_model_prefix)
                ), model
                model_paths.append(model.filename)
        assert len(model_paths) == expected_models, model_paths
        assert any(path.startswith(kicad10_model_prefix) for path in model_paths), model_paths

        api.request(project.SaveDocument(document=fresh_document), Empty)
        fresh_text = fresh_board_path.read_text(encoding="utf-8")
        assert fresh_text.count("(model ") == expected_models
        assert all(f'(model "{path}"' in fresh_text for path in model_paths)
        print("PASS fresh-board netlist import creates footprints with relocatable models", flush=True)


if __name__ == "__main__":
    main()
