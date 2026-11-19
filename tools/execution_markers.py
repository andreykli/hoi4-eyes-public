"""Exact generated result matching, including declared native failure branches."""
import re


def matches_prepared_result(package, status, marker):
    if status not in {'applied', 'failed'} or not isinstance(package, dict) or not isinstance(marker, str):
        return False
    expected = package.get('result_marker' if status == 'applied' else 'failure_marker')
    if not isinstance(expected, str): return False
    if marker == expected: return True
    if status != 'failed': return False
    prefix, _, reason = marker.rpartition('|')
    if reason == 'bundle_partial':
        if not package.get('bundle_term_ids'):
            return False
    elif reason not in {'relation_not_confirmed', 'postcondition_failed'}:
        return False
    if prefix != expected.rpartition('|')[0]: return False
    # Only failure strings literally declared by the saved generated package
    # are accepted. A log line cannot invent a new action, request or reason.
    script = package.get('event_file', '')
    return isinstance(script, str) and marker in re.findall(r'\blog\s*=\s*"([^"\r\n]*)"', script)
