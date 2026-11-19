"""Focus is not a session-exit signal. Unknown menu state never closes a chat."""
def policy(opened, state):
    if state is None: return {'open': opened, 'visible': opened, 'topmost': False}
    ended = state.get('running') is False or state.get('game_state') == 'main_menu'
    keep = bool(opened and not ended)
    return {'open': keep, 'visible': keep,
        'topmost': keep and bool(state.get('game_foreground', state.get('foreground')) or state.get('overlay_foreground'))}
