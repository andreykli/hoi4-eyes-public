"""Release acceptance. Offline fixtures and read-only review of real evidence."""
from argparse import ArgumentParser
from collections import Counter
from contextlib import closing
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import sqlite3
import sys

from user_storage import DATABASE, runtime_dir
from guarded_treaty_effects import NEW_ACTIONS
from state_operations import ACTIONS as STATE_ACTIONS, MARKER as STATE_MARKER

VERSION = "1.19.5.0"
ROOT = Path(__file__).resolve().parents[1]
CASES = {
    "nap": ("non_aggression_pact",),
    "guarantee_access": ("guarantee", "military_access"),
    "embargo": ("embargo", "lift_embargo"),
    "white_peace": ("white_peace",),
    "faction_join": ("faction_join",),
    "faction_leave": ("faction_leave",),
    "territory": ("territory_transfer", "territory_rollback"),
    "compound": ("compound",),
    "country_maker": ("independent", "dependent"),
    "atlas": ("vanilla", "millennium_dawn", "more_provinces"),
    "treaty_cancellations": NEW_ACTIONS,
    "state_operations": tuple(STATE_ACTIONS),
}
MANUAL = {"millennium_dawn", "more_provinces"}
HEX = re.compile(r"[0-9a-f]{32}")
STAMP = re.compile(r"^\[\d\d:\d\d:\d\d\]\[[^\]\r\n]+\]:\s+(EYES_[^\r\n]+)$")


def new_run(campaign):
    if not HEX.fullmatch(campaign):
        raise ValueError("Use the full 32-character Atlas campaign ID.")
    return {"version": VERSION, "campaign": campaign,
            "created_utc": datetime.now(timezone.utc).isoformat(), "evidence": {}}


def read_run(path):
    run = json.loads(path.read_text(encoding="utf-8-sig"))
    if (not isinstance(run, dict) or run.get("version") != VERSION
            or not HEX.fullmatch(str(run.get("campaign", ""))) or not isinstance(run.get("evidence"), dict)):
        raise ValueError("Invalid acceptance run or wrong HOI4 version.")
    return run


def write_json(path, value):
    with path.open("x", encoding="utf-8", newline="\n") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.write("\n")


def record(old, output, check, identity, visual, notes, campaign=None):
    run = read_run(old)
    item = run["evidence"].setdefault(check, {})
    if campaign is not None:
        if not HEX.fullmatch(campaign):
            raise ValueError("Use the full campaign ID for this check.")
        if item.get("id") and item.get("campaign", run["campaign"]) != campaign:
            raise ValueError("This check already binds an attempt in another campaign.")
        item["campaign"] = campaign
    bound_campaign = item.get("campaign", run["campaign"])
    if identity is not None:
        if check in MANUAL:
            raise ValueError("This map check uses a visual verdict and notes.")
        if check == "vanilla" and not re.fullmatch(rf"world:{bound_campaign}:\d+",identity):
            raise ValueError("Use the exact accepted world:<campaign>:<day> snapshot ID.")
        if check not in MANUAL and check != "vanilla" and not HEX.fullmatch(identity):
            raise ValueError("Use the full agreement, proposal or Country Maker ID.")
        if item.get("id") not in (None, identity):
            raise ValueError("Evidence already binds this check. Start a new run for another attempt.")
        item["id"] = identity
    if visual is not None:
        if not notes or not notes.strip():
            raise ValueError("Describe the observed UI result when recording a visual verdict.")
        item.update(visual=visual, notes=notes)
    write_json(output, run)


def log_tail(path):
    if path is None or not path.is_file():
        return []
    with path.open("rb") as stream:
        length = stream.seek(0, 2)
        stream.seek(max(0, length - 16 * 1024 * 1024))
        if stream.tell():
            stream.readline()
        return stream.read().decode("utf-8-sig", errors="replace").splitlines()


def ordered_game_markers(lines):
    events = []
    for line in lines:
        match = STAMP.fullmatch(line)
        if match:
            events.append(match[1])
    return events


def read_evidence(database, campaign):
    if database is None or not database.is_file():
        return {}, {}, {}, {campaign}
    chats, world, sources = {}, {}, {campaign}
    with closing(sqlite3.connect(database.resolve().as_uri()+"?mode=ro", uri=True)) as db:
        db.execute("BEGIN")
        tables = {name for (name,) in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "chat_state" in tables:
            row = db.execute("SELECT schema_version,payload FROM chat_state WHERE singleton=1").fetchone()
            if row:
                if row[0] != 1:
                    raise ValueError("Unsupported SQLite chat-state schema.")
                chats = json.loads(row[1]).get("chats", {})
        if "eyes_world_records" in tables:
            world = {key: (kind, json.loads(body)) for key, kind, body in db.execute(
                "SELECT id,kind,payload FROM eyes_world_records WHERE campaign=?", (campaign,))}
        if "eyes_world_sessions" in tables:
            sources.update(source for (source,) in db.execute(
                "SELECT source FROM eyes_world_sessions WHERE campaign=?", (campaign,)))
    agreements, territories = {}, {}
    for chat in chats.values():
        if chat.get("campaign_id") != campaign:
            continue
        agreements.update((key,value) for key,value in chat.get("agreements", {}).items()
                          if value.get("campaign_id") == campaign)
        territories.update(chat.get("territory_proposals", {}))
    return agreements, territories, world, sources


def marker_count(markers, prefix):
    return sum(count for key, count in markers.items() if key.startswith(prefix))


def agreement_evidence(record, check, markers):
    if check == "compound":
        terms = list(record.get("term_executions", {}).values())
        if len(terms) < 2:
            return False, False, "Need at least two prepared effect receipts."
        observations = [agreement_evidence(term, term.get("action_id"), markers) for term in terms]
        failed = any(item[1] for item in observations)
        ok = record.get("status") == "execution_confirmed" and all(item[0] for item in observations)
        return ok, failed, "Require exact receipts for each term and the final EYES state."
    if record.get("action_id") != check:
        return False, True, "The bound agreement has the wrong action type."
    package = record.get("execution_package") or {}
    marker = package.get("result_marker")
    result = record.get("execution_result") or {}
    count = markers[marker]
    failed_prefix = (package.get("failure_marker") or "").rsplit("|", 1)[0]
    failed = (record.get("status") == "failed" or count > 1
              or bool(failed_prefix and marker_count(markers, failed_prefix+"|")))
    ok = count == 1 and record.get("status") == "execution_confirmed" and result.get("marker") == marker
    return ok, failed, f"Exact HOI4 success lines: {count}. EYES: {record.get('status', 'unknown')}."


def evaluate(check, identity, agreements, territories, world, sources, markers, events):
    if check in MANUAL:
        return True, False, "Manual map review required."
    if check in STATE_ACTIONS:
        kind, record = world.get(identity, (None, {}))
        if kind is None:
            return False, False, "No bound state operation in this campaign."
        if kind != 'state_operation' or record.get('action') != check:
            return False, True, "Bound record has a different state operation."
        prefix = (f"{STATE_MARKER}{identity}|{record['save_token']}|{record['actor']}|"
                  f"{check}|{record['state']}|")
        result = record.get('result') or {}
        success = markers[prefix+f"verified|{result.get('day')}"]
        successes_seen = marker_count(markers,prefix+'verified|')
        failures_seen = marker_count(markers,prefix+'precondition_failed|') + marker_count(markers,prefix+'postcondition_failed|')
        failed = (record.get('status') in {'precondition_failed','postcondition_failed'}
                  or failures_seen>0 or successes_seen>1
                  or (result.get('status')=='verified' and successes_seen>0 and success==0))
        return not failed and success==1 and result.get('status')=='verified', failed, f"Exact state operation markers: {success}."
    if check == "vanilla":
        kind, snapshot = world.get(identity, (None, {}))
        token = snapshot.get("save_token")
        day = snapshot.get("day")
        # EYES stores the accepted snapshot independently of game.log. Require
        # a complete log batch at the selected date and roster/state counts.
        from world_snapshot import Collector
        collector = Collector()
        from hoi4_campaign_identity import campaign_id_from_save_token
        from saved_campaign_binding import matches_bridge_observation
        for marker in events:
            if not marker.startswith("EYES_WORLD_V1|"):
                continue
            try:
                batch = collector.feed(marker)
            except (ValueError, TypeError):
                continue
            if (batch and kind == "world_snapshot" and batch["snapshot"]["day"] == day
                    and (token == batch["campaign_token"] or
                         campaign_id_from_save_token(batch["campaign_token"]) in sources or
                         any(matches_bridge_observation(source,batch["campaign_token"]) for source in sources))
                    and all(batch["snapshot"].get(key) == snapshot.get(key)
                            for key in ("date", "player", "states"))
                    and sorted(batch["snapshot"]["countries"]) == sorted(snapshot.get("countries", []))):
                return True, False, "Exact owners/controllers match a complete log export."
        return False, False, "Select an accepted snapshot ID and supply its complete game.log."
    if check.startswith("territory_"):
        record = territories.get(identity)
        if not record:
            return False, False, "No bound territory proposal in this campaign."
        package = record.get("execution_package") or {}
        prefix = package.get("result_marker_prefix", "")
        rollback = check.endswith("rollback")
        target = "rolled_back" if rollback else "applied"
        result = record.get("rollback_result" if rollback else "execution_result") or {}
        expected = prefix + target + "|" + str(result.get("summary", ""))
        failed = bool(prefix and marker_count(markers,prefix+("rollback_failed" if rollback else "failed")+"|"))
        return bool(prefix and result.get("status") == target and markers[expected] == 1), (
            failed or markers[expected] > 1), "Require exact territory result and persisted EYES record."
    if check in {"dependent", "independent"}:
        kind, record = world.get(identity, (None, {}))
        if kind is None:
            return False, False, "No bound Country Maker request in this campaign."
        if kind != "country_creation" or record.get("country_status") != check:
            return False, True, "Bound request has the wrong country status."
        tag, actor = record.get("new_tag"),record.get("actor")
        matches = [line for line in markers if line.startswith(f"EYES_COUNTRY_MAKER_V1|{identity}|")]
        applied = sum(markers[line] for line in matches
                      if line.split("|")[3:7] == [actor,"applied",tag,str(record.get("result_day"))])
        failed = record.get("status") in {"blocked","partial","rolled_back","rollback_failed"}
        capital = markers[f"EYES_COUNTRY_CAPITAL_V1|{identity}|{tag}|{record.get('capital')}|verified"]
        return (record.get("status") == "applied" and applied == 1 and capital == 1,
                failed or applied > 1 or capital > 1, f"Country markers: {applied}. Capital markers: {capital}.")
    record = agreements.get(identity)
    if record is None:
        return False, False, "No bound agreement in this campaign."
    return agreement_evidence(record, check, markers)


def scoped_news_count(identity, check, agreements, territories, markers):
    if not identity:
        return None
    if check in {"independent","dependent"}:
        event = f"eyes_cm_{identity}.4"
    elif check == "territory_transfer":
        package = territories.get(identity, {}).get("execution_package") or {}
        event_id = package.get("event_id")
        event = event_id.rsplit(".",1)[0]+".3" if event_id else None
    else:
        record = agreements.get(identity) or {}
        if check == "compound":
            records = record.get("term_executions", {}).values()
            return sum(scoped_news_count(identity, value["action_id"],
                                         {identity:value}, {}, markers) or 0 for value in records)
        package = record.get("execution_package") or {}
        event_id = package.get("event_id")
        event = event_id.rsplit(".",1)[0]+".2" if event_id else None
    return markers[f"EYES_HOI4_NEWS_ACK_V1|{event}"] if event else None


def assess(run, database, game_log, bridge_log, eyes_log):
    paths = game_log if isinstance(game_log,(tuple,list)) else [game_log]
    events = [event for path in dict.fromkeys(paths) for event in ordered_game_markers(log_tail(path))]
    markers = Counter(events)
    ledgers = {run["campaign"]:read_evidence(database,run["campaign"])}
    bridge, eyes = log_tail(bridge_log), log_tail(eyes_log)
    rows = {}
    for scenario, keys in CASES.items():
        parts = {}
        for check in keys:
            evidence = run["evidence"].get(check, {})
            identity = evidence.get("id")
            campaign = evidence.get("campaign",run["campaign"])
            if campaign not in ledgers:
                ledgers[campaign] = read_evidence(database,campaign)
            agreements,territories,world,sources = ledgers[campaign]
            ok, failed, info = evaluate(check,identity,agreements,territories,world,sources,markers,events)
            news_acks = scoped_news_count(identity,check,agreements,territories,markers)
            if news_acks is not None and news_acks>1:
                failed, info = True, info+" Repeated in-game news acknowledgement."
            visual = evidence.get("visual")
            status = ("FAILED" if failed or visual == "failed" else
                      "PASSED" if ok and visual == "passed" and evidence.get("notes") else "NOT_RUN")
            parts[check] = {"status": status, "machine_evidence":None if check in MANUAL else ok,"visual":visual,"news_ack_count":news_acks,"detail":info}
        statuses = {row["status"] for row in parts.values()}
        rows[scenario] = {"status": "FAILED" if "FAILED" in statuses else
                          "PASSED" if statuses == {"PASSED"} else "NOT_RUN", "checks":parts}
    totals = Counter(row["status"] for row in rows.values())
    return {"campaign":run["campaign"],"target_version":VERSION,"scenarios":rows,
            "summary":{code:totals[code] for code in ("PASSED","FAILED","NOT_RUN")},
            "available_ids":{
                campaign:{
                    "agreements":[{"id":key,"action":value.get("action_id"),"status":value.get("status")}
                                  for key,value in ledger[0].items()],
                    "territory_proposals":list(ledger[1]),
                    "snapshots":[key for key,(kind,_) in ledger[2].items() if kind=="world_snapshot"],
                    "country_requests":[key for key,(kind,_) in ledger[2].items() if kind=="country_creation"],
                    "state_operations":[key for key,(kind,_) in ledger[2].items() if kind=="state_operation"],
                } for campaign,ledger in ledgers.items()
            },
            "diagnostics": {
                "scope": "Bridge and Flask log counts are informational; SQLite receipts are campaign-scoped.",
                "bridge_lines":len(bridge),"bridge_error_lines":sum("error" in line.casefold() for line in bridge),
                "eyes_lines":len(eyes),"eyes_result_http_lines":sum(
                    any(route in line for route in ("/agreement-executions/result","/territory-results",
                                                   "/world/country-result","/world/state-operation-result","/world/snapshots")) for line in eyes),
                "news_ack_lines":marker_count(markers,"EYES_HOI4_NEWS_ACK_V1|"),
            }}


def scan_run(campaign, database, game_logs):
    """Preselect evidence only when one stored action matches this branch and log."""
    run = new_run(campaign)
    agreements, territories, world, sources = read_evidence(database, campaign)
    events = [event for path in dict.fromkeys(game_logs) for event in ordered_game_markers(log_tail(path))]
    markers = Counter(events)
    for check in (key for keys in CASES.values() for key in keys if key not in MANUAL):
        if check in STATE_ACTIONS:
            ids = [key for key,(kind,row) in world.items() if kind=='state_operation' and row.get('action')==check]
        elif check in {'independent','dependent'}:
            ids = [key for key,(kind,row) in world.items() if kind=='country_creation' and row.get('country_status')==check]
        elif check == 'vanilla':
            ids = [key for key,(kind,_) in world.items() if kind=='world_snapshot']
        elif check.startswith('territory_'):
            ids = list(territories)
        else:
            ids = [key for key,row in agreements.items()
                   if (check=='compound' and row.get('term_executions')) or row.get('action_id')==check]
        matching = []
        for identity in ids:
            ok, failed, _ = evaluate(check,identity,agreements,territories,world,sources,markers,events)
            if ok or failed:
                matching.append(identity)
        if check=='vanilla' and matching:
            matching = [max(matching,key=lambda key:world[key][1]['day'])]
        if len(matching)==1:
            run['evidence'][check] = {'id':matching[0]}
    return run


def export_approved(run, database, agreement_id, campaign, output):
    from treaty_package_install import validate_package
    if output.suffix.lower() != ".json" or (campaign is not None and not HEX.fullmatch(campaign)):
        raise ValueError("Export to a new JSON file for a full campaign ID.")
    agreements, _, _, _ = read_evidence(database,campaign or run["campaign"])
    agreement = agreements.get(agreement_id) or {}
    delivery = agreement.get("native_delivery") or {}
    packet = delivery.get("packet")
    if (agreement.get("status") != "manual_required" or agreement.get("execution_result")
            or delivery.get("sent") or not isinstance(packet,dict)
            or packet.get("agreement_id") != agreement_id
            or packet.get("campaign_id") != agreement.get("campaign_id")
            or packet.get("version") != agreement.get("current_version")
            or agreement.get("approved_version") != agreement.get("current_version")):
        raise ValueError("No frozen pending package for that approved version. Prepare it in EYES with a fresh Atlas export.")
    validate_package(packet)
    write_json(output, packet)


def main():
    parser = ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    init = commands.add_parser("init")
    init.add_argument("--campaign", required=True)
    init.add_argument("--output", type=Path, required=True)
    add = commands.add_parser("record")
    add.add_argument("--run", type=Path, required=True)
    add.add_argument("--output", type=Path, required=True)
    add.add_argument("--check", choices=[key for keys in CASES.values() for key in keys], required=True)
    add.add_argument("--id")
    add.add_argument("--campaign")
    add.add_argument("--visual", choices=("passed", "failed"))
    add.add_argument("--notes")
    result = commands.add_parser("status")
    result.add_argument("--run", type=Path, required=True)
    result.add_argument("--db", type=Path, default=runtime_dir()/DATABASE)
    result.add_argument("--game-log", type=Path, action="append",
                        help="Supply once per archived game session in chronological order.")
    result.add_argument("--output", type=Path)
    result.add_argument("--bridge-log", type=Path, default=runtime_dir()/"bridge.log")
    result.add_argument("--eyes-log", type=Path, default=runtime_dir()/"flask.log")
    export = commands.add_parser("export-approved")
    export.add_argument("--run", type=Path, required=True)
    export.add_argument("--db", type=Path, default=runtime_dir()/DATABASE)
    export.add_argument("--campaign")
    export.add_argument("--agreement", required=True)
    export.add_argument("--output", type=Path, required=True)
    prepare = commands.add_parser("prepare-offline")
    prepare.add_argument("--game-dir", type=Path, required=True)
    prepare.add_argument("--output", type=Path, required=True)
    scan = commands.add_parser("scan-game")
    scan.add_argument("--campaign")
    scan.add_argument("--db",type=Path,default=runtime_dir()/DATABASE)
    scan.add_argument("--game-log",type=Path,action="append")
    args = parser.parse_args()
    try:
        if args.command == "init":
            write_json(args.output,new_run(args.campaign))
        elif args.command == "record":
            record(args.run,args.output,args.check,args.id,args.visual,args.notes,args.campaign)
        elif args.command == "status":
            game_logs = args.game_log or [Path.home()/"Documents/Paradox Interactive/Hearts of Iron IV/logs/game.log"]
            report = assess(read_run(args.run),args.db,game_logs,args.bridge_log,args.eyes_log)
            if args.output:
                write_json(args.output,report)
            print(json.dumps(report,ensure_ascii=True,indent=2))
        elif args.command == "export-approved":
            export_approved(read_run(args.run),args.db,args.agreement,args.campaign,args.output)
        elif args.command == "scan-game":
            from uuid import uuid4
            campaign = args.campaign or input("Atlas test campaign ID: ").strip()
            logs = args.game_log or [Path.home()/"Documents/Paradox Interactive/Hearts of Iron IV/logs/game.log"]
            run = scan_run(campaign,args.db,logs)
            parent=ROOT/"runtime"/"tmp"/("game-evidence-"+uuid4().hex)
            parent.mkdir(parents=True)
            write_json(parent/"run.json",run)
            report = assess(run,args.db,logs,runtime_dir()/"bridge.log",runtime_dir()/"flask.log")
            write_json(parent/"report.json",report)
            print(json.dumps(report["summary"]))
            print(parent/"report.json")
        else:
            from release_acceptance_offline import prepare_offline
            info=prepare_offline(args.game_dir,args.output)
            print(json.dumps(info,ensure_ascii=True,indent=2))
            return 0 if info["passed"] else 1
    except (OSError, EOFError, ValueError, sqlite3.Error, KeyError, TypeError) as exc:
        print(str(exc),file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
