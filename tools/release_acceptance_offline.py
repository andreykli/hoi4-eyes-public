"""Isolated acceptance scenarios using the existing regression fixtures and builders."""
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
import io
import json
import tempfile
import unittest

from hoi4_static_audit import audit
from release_acceptance import ROOT, write_json
from validate_hoi4_gui import parse_pdx

SUITES = {
    "nap": ["test_treaty_decision_package", "test_treaty_ai_execution"],
    "guarantee_access": ["test_expansion.ExpansionTests.test_existing_native_actions_compile_without_execution"],
    "embargo": ["test_expansion.ExpansionTests.test_native_action_static_audit"],
    "white_peace": ["test_guarded_treaty_effects"],
    "faction_join": ["test_guarded_treaty_effects"],
    "faction_leave": ["test_guarded_treaty_effects"],
    "territory": ["test_territory_execution"],
    "compound": ["test_agreements.DiplomaticAgreementLifecycleTests.test_compound_effects_wait_for_every_term_marker_before_signing",
                 "test_agreements.DiplomaticAgreementLifecycleTests.test_composite_contract_with_paper_only_clause_is_blocked_as_a_whole"],
    "country_maker": ["test_country_maker_execution"],
    "atlas": ["test_atlas_map_sources", "test_world_snapshot", "test_unified_atlas"],
    "bridge_memory_news": ["test_bridge_recovery", "test_diplomatic_memory", "test_news_verification"],
    "state_operations": ["test_state_operations"],
}


class DetailedResult(unittest.TextTestResult):
    def __init__(self, *args):
        super().__init__(*args)
        self.cases = []

    def stopTest(self, test):
        key = test.id()
        failures = [(case, error) for case, error in self.failures + self.errors
                    if case.id().split(' (')[0] == key]
        skipped = [reason for case, reason in self.skipped if case.id().split(' (')[0] == key]
        self.cases.append({"test":key, "status":"failed" if failures else "skipped" if skipped else "passed",
                           "details":[error for _,error in failures] or skipped})
        super().stopTest(test)


def fixture_packets(game):
    from country_maker_execution import compile_country
    from guarded_treaty_effects import EFFECTS, build_package
    from player_native_actions import compile_action
    from test_agreements import DiplomaticAgreementLifecycleTests
    from test_country_maker_execution import PLAN, SNAPSHOT, TOKEN as COUNTRY_TOKEN
    from test_expansion import prepared
    from test_guarded_treaty_effects import GuardedTreatyTests
    from test_territory_execution import TerritoryExecutionPackageTests
    from test_treaty_decision_package import TOKEN, approved
    from treaty_decision_package import compile_decision
    from state_operations import ACTIONS, prepare
    from test_state_operations import OBSERVATION

    yield "nap", compile_decision(approved(), save_token=TOKEN, not_after_day=100)["files"]
    for action in ("guarantee", "military_access", "embargo", "lift_embargo"):
        record, chat = prepared(action)
        yield action, compile_action(record, chat, save_token=TOKEN, not_after_day=100)["files"]
    for action in EFFECTS:
        record, chat = GuardedTreatyTests().fixture(action)
        record["execution_package"] = build_package(chat, record, record["versions"][0], record["execution_request_id"])
        record["status"] = "manual_required"
        yield action, compile_action(record, chat, save_token=TOKEN, not_after_day=100)["files"]
    engine, chat, proposal, _ = TerritoryExecutionPackageTests().approved_engine_and_proposal()
    package = engine.prepare_territory_execution(chat, proposal)
    yield "territory", {f'events/{package["event_file_name"]}': package["event_file"]}
    engine, chat, agreement, record = DiplomaticAgreementLifecycleTests()._composite_execution_fixture()
    current = record["versions"][0]
    current["terms"] = [term for term in current["terms"] if term["supported_by_hoi4"]]
    engine.prepare_agreement_execution(chat, agreement, request_id="qa-compound")
    files = {}
    for term in current["terms"]:
        prepared_term = engine.prepare_agreement_term_execution(
            chat, agreement, term_id=term["term_id"], request_id=f'qa-{term["term_id"]}')["prepared_term"]
        package = prepared_term["execution_package"]
        files[f'events/{package["event_file_name"]}'] = package["event_file"]
        engine.record_agreement_execution_result(chat, agreement, request_id=package["execution_request_id"],
                                                 status="applied", marker=package["result_marker"])
    yield "compound", files
    for status, identifier in (("independent","1"*32), ("dependent","2"*32)):
        country = compile_country(dict(PLAN,status=status), SNAPSHOT, COUNTRY_TOKEN,
                                  request_id=identifier, game_root=game)
        yield status, {name: content for name, content in country["files"].items() if isinstance(content, str)}
    for action in ACTIONS:
        parameters = {'previous_capital':13} if action=='move_capital' else (
                     {'current_level':2,'level':3} if action=='adjust_infrastructure' else {})
        _, packet = prepare(action, 12, OBSERVATION, **parameters)
        yield action, packet["files"]


def prepare_offline(game, output):
    output = output.resolve()
    if not output.is_relative_to((ROOT/"runtime/tmp").resolve()):
        raise ValueError("Offline acceptance artifacts belong under runtime/tmp.")
    output.mkdir()
    if not (ROOT/'tools/test_agreements.py').is_file():
        # Public distributions deliberately omit developer fixtures and tests.
        from release_smoke_checks import check_distribution
        return check_distribution(ROOT, game, output)
    packets = {}
    for case, files in fixture_packets(game):
        with tempfile.TemporaryDirectory(dir=output) as temporary:
            for name, content in files.items():
                path = Path(temporary)/name
                path.parent.mkdir(parents=True,exist_ok=True)
                path.write_text(content,encoding="utf-8")
                if name.startswith("events/"):
                    parse_pdx(content)
            findings = audit(temporary, game)
        # These are script samples, not installer payloads. Never place them
        # under the game's event trees or attach them to an active campaign.
        write_json(output/f"{case}.offline.json",
                   {"fixture_only":True,"game_effect_applied":False,"sample_files":files})
        packets[case] = {"files":len(files),"errors":findings["errors"],"warnings":findings["warnings"]}
    results, saved = {}, {}
    for key, names in SUITES.items():
        fingerprint = tuple(names)
        if fingerprint not in saved:
            buffer = io.StringIO()
            with redirect_stdout(buffer), redirect_stderr(buffer):
                result = unittest.TextTestRunner(stream=buffer,verbosity=2,resultclass=DetailedResult).run(
                    unittest.defaultTestLoader.loadTestsFromNames(names))
            (output/f"{key}.log").write_text(buffer.getvalue(),encoding="utf-8")
            saved[fingerprint] = {"ran":result.testsRun,"failed":len(result.failures)+len(result.errors),
                                  "skipped":len(result.skipped),"passed":result.wasSuccessful(),"tests":result.cases}
        results[key] = saved[fingerprint]
    launcher = game/"launcher-settings.json"
    installed = json.loads(launcher.read_text(encoding="utf-8-sig")).get("rawVersion") if launcher.is_file() else None
    summary = {"target_version":"1.19.5.0","installed_version":installed,"scope":"offline only","gameplay_confirmed":False,
               "runtime_evidence":"not_run", "manual_review":list(packets),
               "unique_tests":len({test["test"] for group in saved.values() for test in group["tests"]}),
               "packets":packets,"scenarios":results,
               "passed":all(item["passed"] for item in results.values())
                         and not any(item["errors"] for item in packets.values())}
    write_json(output/"offline-report.json",summary)
    return summary
