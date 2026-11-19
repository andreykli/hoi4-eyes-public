# Copyright (c) 2026 Voices of History — HOI4 AI Mod contributors
# SPDX-License-Identifier: MIT
# Derived from middleware/game_state.py at 3226478a81edfc376d63502fa9708c48d309b447.
# Original function bodies below are retained verbatim. EYES adaptation lives in voices_adapter.py.
# Full upstream permission and disclaimer: LICENSE in this directory.
"""Selected, dependency-free Voices of History context implementation."""
import re

def diplomatic_contact(info, state, world=None):
    """Resolve a foreign chat against the synchronized player and latest nation export."""
    player = str(state.get("tag", "")).upper()
    target = str(info.get("target", "")).upper()
    logged_player = str(info.get("player", "")).upper()
    if not all(re.fullmatch(r"[A-Z][A-Z0-9]{2}", tag)
               for tag in (player, target, logged_player)):
        raise ValueError("Sync the current campaign before opening a foreign-country chat.")
    if logged_player != player:
        raise ValueError("The diplomatic request belongs to another player country; reopen diplomacy.")
    if target == player:
        raise ValueError("Select another country for diplomacy; use the Head of State Office for your own.")
    nation = (world or {}).get(target, {})
    return {"target": target, "player": player,
            "tname": nation.get("name") or info.get("tname") or target,
            "tleader": nation.get("leader") or info.get("tleader") or "Unknown representative"}



def diplomatic_context(state, world, info):
    """Always include the chosen counterpart, even when it is a minor outside the key roster."""
    contact = diplomatic_contact(info, state, world)
    nation = (world or {}).get(contact["target"], {})
    def flag(key):
        return {"1": "Yes", "0": "No"}.get(nation.get(key), "Unknown")
    lines = ["=== DIPLOMATIC COUNTERPART ===",
             f"You represent: {contact['tname']} ({contact['target']})",
             f"Your leader/representative: {contact['tleader']}",
             f"The human player represents: {state.get('name', '?')} ({contact['player']})",
             f"Current campaign date: {state.get('date', '?')}",
             f"Counterpart snapshot date: {nation.get('date', 'not exported')}",
             f"Your ruling ideology/outlook: {nation.get('ideology_name', 'Unknown')}",
             f"At war with the player: {flag('waryou')}",
             f"In the same faction as the player: {flag('facyou')}",
             f"Subject of the player: {flag('subjyou')}",
             f"Subject of another country: {flag('subj')}",
             f"Borders the player: {flag('nb')}",
             f"Capitulated: {flag('cap')}"]
    if nation:
        lines.append("Your exported national situation: " + _nation_line(nation))
    else:
        lines.append("National situation unavailable. Do not invent government or diplomatic facts.")
    lines += ["Player-state statistics belong to the player, not to your country.",
              "Missing treaties, opinion values, trade, stockpiles and military positions are unknown.",
              "Use the counterpart snapshot date to judge freshness; reopen diplomacy to refresh it.",
              "================================"]
    return "\n".join(lines)



def _nation_line(n):
    """Una riga leggibile e compatta per l'IA, SOLO con fatti reali esportati dal gioco."""
    flags = []
    if n.get("cap") == "1":
        flags.append("CAPITULATED")
    if n.get("waryou") == "1":
        flags.append("AT WAR WITH YOU")
    elif n.get("war") == "1":
        flags.append("at war")
    if n.get("facyou") == "1":
        flags.append("your faction")
    if n.get("subjyou") == "1":
        flags.append("YOUR SUBJECT")
    elif n.get("subj") == "1":
        flags.append("a subject")
    if n.get("maj") == "1":
        flags.append("great power")
    if n.get("nb") == "1":
        flags.append("borders you")
    tail = ("  [" + ", ".join(flags) + "]") if flags else ""
    return (f"- {n.get('name','?')} ({n.get('tag','?')}): {n.get('ideology_name','?')}, "
            f"stab {n.get('stab','?')}%, war supp {n.get('ws','?')}%, "
            f"factories {n.get('civ','?')}c/{n.get('mil','?')}m, "
            f"{n.get('div','?')} divisions, {n.get('states','?')} states{tail}")



def _nation_compact(n):
    """Riga compatta (roster di TUTTE le nazioni): nome(tag) + bandierine essenziali."""
    fl = []
    if n.get("waryou") == "1":
        fl.append("war-vs-you")
    elif n.get("war") == "1":
        fl.append("at war")
    if n.get("cap") == "1":
        fl.append("capitulated")
    if n.get("subjyou") == "1":
        fl.append("your subject")
    elif n.get("subj") == "1":
        fl.append("subject")
    return f"{n.get('name','?')} ({n.get('tag','?')})" + ("[" + ", ".join(fl) + "]" if fl else "")



def world_snapshot_block(world):
    """Blocco WORLD SNAPSHOT dal dict {tag: nation}: la VERITA' di base sul mondo.
    Nessun limite di rilevanza: l'IA deve sapere di TUTTI. Per non perdere contesto ne'
    esplodere, le nazioni CHIAVE (in guerra, grandi potenze, vicini, fazione, sudditi) sono
    in DETTAGLIO, e TUTTE le altre in un roster COMPATTO raggruppato per ideologia."""
    if not world:
        return ("WORLD SNAPSHOT: not available yet (it is exported when you open a channel "
                "or each in-game day).")
    nats = list(world.values())

    def is_key(n):
        return (n.get("waryou") == "1" or n.get("maj") == "1" or n.get("nb") == "1"
                or n.get("facyou") == "1" or n.get("subjyou") == "1" or n.get("war") == "1")

    def keyf(n):
        score = 0
        if n.get("waryou") == "1":
            score -= 100
        if n.get("maj") == "1":
            score -= 10
        if n.get("nb") == "1":
            score -= 5
        if n.get("subjyou") == "1":
            score -= 3
        return (score, n.get("name", ""))

    key_nats = sorted([n for n in nats if is_key(n)], key=keyf)
    detailed, overflow = key_nats[:45], key_nats[45:]
    others = overflow + [n for n in nats if not is_key(n)]

    out = ["WORLD SNAPSHOT — real, current state of the world (GROUND TRUTH: these are the ONLY "
           "facts about other nations; never assume or invent anything beyond them).",
           "KEY nations (in detail):"]
    out += [_nation_line(n) for n in detailed]

    if others:
        groups = {}
        for n in others:
            groups.setdefault(n.get("ideology_name", "Unknown"), []).append(n)
        out.append("ALL OTHER nations (compact roster — name(TAG), flags only if notable):")
        # Overhauls define their own political groups. Include every exported label.
        for ideo in sorted(groups):
            grp = groups.get(ideo)
            if not grp:
                continue
            ents = ", ".join(_nation_compact(n) for n in sorted(grp, key=lambda x: x.get("name", "")))
            out.append(f"  {ideo}: {ents}")
    return "\n".join(out)

