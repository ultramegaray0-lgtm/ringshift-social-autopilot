"""Weekly candidate levels for Ringshift, proven with the game's own tracer, offered as a pull request. Never merged here.

    python scripts/level_lab.py run --game-dir game-src --out lab-out            # generate + validate + previews
    python scripts/level_lab.py run --game-dir game-src --out lab-out --open-pr  # ...and push a branch + open a PR

How a candidate is made (all inside the real game page, with the game's own traceAllBeams):
1. A "concept" picks 2-3 mechanics (e.g. Portal + Prism). A board is laid out in the game's real level schema (12 slots,
   3 rings, 1-2 spokes per ring, gimmick encodings copied from existing levels so every field is one the game uses).
2. Every rotation of every ring is traced (12^3 = 1,728 boards). Required locks and the bonus (coin) lock are then
   chosen so that EXACTLY ONE rotation lights all of them (one coin solution), at most a few light the required ones
   alone, and the start position lights none of the required locks.
3. Each gimmick is removed in turn and the winning board re-traced: a gimmick whose removal breaks the solution is "on
   the route". A candidate needs at least 3 route gimmicks of at least 2 different mechanics, a solution of 6-14 moves,
   and a structure that no existing level has. Candidates are scored by route length, mechanic variety, near-miss
   boards (wrong rotations that light all but one lock) and solution length.

How it is validated (in a fresh browser, on the PATCHED build):
* the page loads with no JavaScript errors and still exposes what the capture tool needs;
* every existing level in every mode is byte-for-byte unchanged (JSON of each level compared with the stable build);
* the game's solver (the same exhaustive search the capture tool uses) finds exactly one coin solution, equal to the
  generator's, and the start position is not solved;
* a real keyboard playthrough of the solution makes the game itself report the win AND record the coin in the save;
* the level grid renders the new level, and a puzzle screenshot passes the still-usability check.

The candidates go into a marked block appended after the LEVELS object (LEVELS.hard.push(...)), so existing ids and
order never change. The patched build is pushed to a NEW branch of the game repository and a pull request is opened for
a person to play and merge. Nothing is ever pushed to the game's main branch by this tool.
"""

import argparse
import json
import os
import random
import shutil
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import capture_gameplay as cg
import game_patch
import game_source
import game_repo

BASE_DIR = Path(__file__).resolve().parent.parent
BLOCK_NAME = "AUTO_LEVEL_CANDIDATES"
TARGET_MODE = "hard"
DEFAULT_COUNT = 2
DEFAULT_BUDGET_S = 240

CONCEPTS = [
    ["portal", "mirror"], ["prism", "half_block"], ["amp", "block", "mirror"], ["echo_portal", "mirror"],
    ["prism", "portal"], ["half_block", "amp", "mirror"], ["echo_portal", "prism"], ["portal", "half_block", "block"],
    ["mirror", "prism", "block"], ["echo_portal", "half_block"],
]
CONCEPT_NAMES = {"portal": "Portal", "mirror": "Mirror", "prism": "Prism", "half_block": "Half-Block", "amp": "Amplifier",
                 "block": "Block", "echo_portal": "Echo"}

GEN_JS = r"""(cfg)=>{
  const started = performance.now();
  let seed = cfg.seed >>> 0;
  const rnd = ()=>{ seed = (seed + 0x6D2B79F5) | 0; let t = Math.imul(seed ^ (seed >>> 15), 1 | seed);
                    t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t; return ((t ^ (t >>> 14)) >>> 0) / 4294967296; };
  const pick = a => a[Math.floor(rnd() * a.length)];
  const shuffle = a => { for(let i = a.length - 1; i > 0; i--){ const j = Math.floor(rnd() * (i + 1)); [a[i], a[j]] = [a[j], a[i]]; } return a; };
  const clone = x => JSON.parse(JSON.stringify(x));
  const N = cfg.slots, R = cfg.rings, ALL = [...Array(N).keys()], FULL = (1 << N) - 1;
  const typeOf = raw => typeof raw === 'string' ? raw : raw.type;
  const fold = t => (t === 'mirror_r' || t === 'mirror_l') ? 'mirror' : t;

  // Real gimmick encodings, copied from the game's own Easy/Normal/Hard levels.
  const samples = {};
  for(const m of ['easy','normal','hard']) for(const l of LEVELS[m]) for(const ring of l.rings)
    for(const raw of Object.values(ring.gimmicks || {})) (samples[typeOf(raw)] = samples[typeOf(raw)] || []).push(raw);
  const sampleOf = t => clone(pick(samples[t]));

  // Structure signatures of every existing level (sorted gimmick kinds per ring + shape): a candidate must differ.
  const sig = l => [l.slots, l.rings.length, l.lockSlots.length,
    l.rings.map(r => Object.values(r.gimmicks || {}).map(g => fold(typeOf(g))).sort().join('+')).sort().join('|')].join('/');
  const existing = new Set();
  for(const m of Object.keys(LEVELS)) for(const l of LEVELS[m]) if(l && l.rings) existing.add(sig(l));

  const popcount = x => { let c = 0; while(x){ x &= x - 1; c++; } return c; };
  const maskOf = set => { let m = 0; set.forEach(s => { if(s >= 0 && s < N) m |= (1 << s); }); return m; };
  const trace = (lvl, rots) => maskOf(traceAllBeams({slots: N, rings: lvl.rings, lockSlots: ALL}, rots, undefined, undefined).litLocks);
  const rotsOf = idx => { const r = []; for(let k = 0; k < R; k++){ r.push(idx % N); idx = Math.floor(idx / N); } return r; };
  const dist = (a, b) => { let d = ((b - a) % N + N) % N; return Math.min(d, N - d); };

  function layout(concept){
    const rings = [];
    for(let r = 0; r < R; r++){
      const spokes = shuffle(ALL.slice()).slice(0, rnd() < 0.5 ? 1 : 2);
      rings.push({spokeSlots: spokes, gimmicks: {}, initialRotation: 0});
    }
    const free = r => ALL.filter(s => !rings[r].spokeSlots.includes(s) && rings[r].gimmicks[s] === undefined);
    const place = (r, raw) => { const f = free(r); if(!f.length) return false; rings[r].gimmicks[pick(f)] = raw; return true; };
    const total = cfg.minGimmicks + Math.floor(rnd() * (cfg.maxGimmicks - cfg.minGimmicks + 1));
    let placed = 0;
    for(const t of concept){
      if(t === 'portal'){
        const a = Math.floor(rnd() * R), b = Math.min(R - 1, Math.max(0, a + pick([-1, 0, 1])));
        const p1 = sampleOf('portal'), p2 = sampleOf('portal'); p1.id = 'P0'; p2.id = 'P0';
        if(!place(a, p1) || !place(b, p2)) return null;
        placed += 2;
      } else if(t === 'mirror'){
        if(!place(Math.floor(rnd() * R), sampleOf(pick(['mirror_l', 'mirror_r'])))) return null; placed++;
      } else { if(!place(Math.floor(rnd() * R), sampleOf(t))) return null; placed++; }
    }
    const extras = concept.filter(t => t !== 'portal');
    while(placed < total && extras.length){
      const t = pick(extras);
      if(!place(Math.floor(rnd() * R), t === 'mirror' ? sampleOf(pick(['mirror_l', 'mirror_r'])) : sampleOf(t))) break;
      placed++;
    }
    return {name: 'CANDIDATE', slots: N, rings, lockSlots: [], bonusLockSlots: []};
  }

  const out = []; let tried = 0, traced = 0;
  const TOTAL = Math.pow(N, R);
  while(performance.now() - started < cfg.budgetMs && out.length < cfg.want){
    tried++;
    const concept = pick(cfg.concepts);
    const lvl = layout(concept);
    if(!lvl) continue;
    if(existing.has(sig(Object.assign({}, lvl, {lockSlots: new Array(cfg.locks)})))) continue;
    const masks = new Int32Array(TOTAL);
    for(let i = 0; i < TOTAL; i++) masks[i] = trace(lvl, rotsOf(i));
    traced += TOTAL;

    let best = null;
    const order = shuffle([...Array(TOTAL).keys()]).filter(i => popcount(masks[i]) >= cfg.locks + 1).slice(0, 60);
    for(const T of order){
      const bits = ALL.filter(s => masks[T] & (1 << s));
      for(let attempt = 0; attempt < 6; attempt++){
        const chosen = shuffle(bits.slice());
        const req = chosen.slice(0, cfg.locks).sort((a, b) => a - b), coin = chosen[cfg.locks];
        const S = maskOf(req), C = S | (1 << coin);
        let nC = 0, nS = 0, near = 0;
        for(let i = 0; i < TOTAL; i++){
          const m = masks[i];
          if((m & C) === C) nC++;
          if((m & S) === S) nS++;
          else if(popcount(m & C) === cfg.locks) near++;
        }
        if(nC !== 1 || nS > cfg.maxNoCoin) continue;
        const target = rotsOf(T);
        // Start: lights no required lock, solution length in range, as long as allowed.
        const starts = [];
        for(let i = 0; i < TOTAL; i++){
          if(masks[i] & S) continue;
          const r = rotsOf(i); let cost = 0; for(let k = 0; k < R; k++) cost += dist(r[k], target[k]);
          if(cost >= cfg.minCost && cost <= cfg.maxCost && r.every((v, k) => dist(v, target[k]) >= 1)) starts.push([cost, r]);
        }
        if(!starts.length) continue;
        starts.sort((a, b) => b[0] - a[0]);
        const [cost, start] = pick(starts.slice(0, Math.max(1, Math.ceil(starts.length / 3))));
        // Which gimmicks the solution really needs.
        const route = [];
        lvl.rings.forEach((ring, r) => Object.keys(ring.gimmicks).forEach(slot => {
          const trial = clone(lvl); delete trial.rings[r].gimmicks[slot];
          if((trace(trial, target) & C) !== C) route.push({ring: r + 1, slot: +slot, kind: fold(typeOf(ring.gimmicks[slot]))});
        }));
        const routeKinds = [...new Set(route.map(g => g.kind))];
        if(route.length < cfg.minRoute || routeKinds.length < 2) continue;
        const score = route.length * 3 + routeKinds.length * 5 + Math.min(near, 40) / 4 + cost + (nS === 1 ? 3 : 0);
        if(!best || score > best.score){
          const level = clone(lvl);
          level.rings.forEach((ring, k) => ring.initialRotation = start[k]);
          level.lockSlots = req; level.bonusLockSlots = [coin];
          best = {level, concept, target, start, cost, coinSolutions: nC, requiredOnlySolutions: nS - nC, nearMisses: near,
                  route, routeKinds, score, rotations: TOTAL};
        }
        break;
      }
    }
    if(best) out.push(best);
  }
  return {candidates: out, tried, traced, ms: Math.round(performance.now() - started)};
}"""

LEVEL_JSON_JS = """()=>{ const out = {}; for(const m of Object.keys(LEVELS)) out[m] = (LEVELS[m]||[]).map(l=>JSON.stringify(l)); return out; }"""


def utc_now():
    return datetime.now(timezone.utc)


def generate(game_path, *, count=DEFAULT_COUNT, budget_s=DEFAULT_BUDGET_S, seed=None, log=print):
    """Candidate levels from the real game's tracer. Returns a list of candidate dicts (best first)."""
    rng = random.Random(seed)
    found, tried, traced = [], 0, 0
    deadline = time.monotonic() + budget_s
    with cg.GameSession(game_path) as session:
        while time.monotonic() < deadline and len({tuple(sorted(c['routeKinds'])) for c in found}) < count + 1 and len(found) < count * 12:
            chunk_ms = int(min(30, max(5, deadline - time.monotonic())) * 1000)
            result = session.page.evaluate(GEN_JS, {
                "seed": rng.randrange(1, 2 ** 31), "slots": 12, "rings": 3, "locks": rng.choice((3, 3, 4)),
                "minGimmicks": 3, "maxGimmicks": 6, "maxNoCoin": 3, "minCost": 6, "maxCost": 14, "minRoute": 3,
                "concepts": CONCEPTS, "budgetMs": chunk_ms, "want": 1})
            tried += result["tried"]
            traced += result["traced"]
            found += result["candidates"]
            if session.errors:
                raise cg.CaptureError("the game logged errors while generating: " + session.errors[0][:160])
    log(f"Generator: {tried} layouts tried, {traced:,} boards traced, {len(found)} candidate(s) passed")
    return pick_varied(found, count)


def pick_varied(found, count):
    """The best candidates, preferring a different mix of mechanics on the route for each one (a batch of the same
    idea twice is less useful to review than two different ideas)."""
    ranked, seen_layouts = [], set()
    for candidate in sorted(found, key=lambda c: -c["score"]):
        key = json.dumps(candidate["level"]["rings"], sort_keys=True)
        if key not in seen_layouts:
            seen_layouts.add(key)
            ranked.append(candidate)
    chosen, mixes = [], set()
    for candidate in ranked:
        mix = tuple(sorted(candidate["routeKinds"]))
        if mix not in mixes:
            chosen.append(candidate)
            mixes.add(mix)
        if len(chosen) == count:
            return chosen
    return (chosen + [c for c in ranked if c not in chosen])[:count]


def candidate_comment(candidate, build, today):
    level = candidate["level"]
    kinds = " + ".join(CONCEPT_NAMES.get(k, k) for k in candidate["routeKinds"])
    route = ", ".join(f"R{g['ring']}:{CONCEPT_NAMES.get(g['kind'], g['kind'])}" for g in candidate["route"])
    return "\n".join([
        f"  // ---- CANDIDATE (level lab {today}) -- {kinds}. Generated and verified by ringshift-social-autopilot level_lab.py.",
        f"  // VERIFIED (build {build}, real tracer, all 12^{len(level['rings'])} = {candidate['rotations']:,} rotations): exactly "
        f"{candidate['coinSolutions']} coin rotation {candidate['target']}; {candidate['requiredOnlySolutions']} no-coin rotation(s).",
        f"  // On the route (removing any one breaks the solution): {route}.",
        f"  // Start {candidate['start']} lights 0 required locks; shortest solution {candidate['cost']} moves; "
        f"{candidate['nearMisses']} wrong rotations light all but one of the locks.",
    ])


def patch_source(source, candidates, *, build, today):
    """The stable build with the candidates appended to LEVELS.hard in a marked block after the LEVELS object."""
    entries = []
    for candidate in candidates:
        level = dict(candidate["level"])
        entries.append(candidate_comment(candidate, build, today) + "\n  " + game_patch.js_literal(level))
    body = (f"// Candidate levels appended to {TARGET_MODE.title()}. Existing levels, their order and their ids are untouched.\n"
            f"LEVELS.{TARGET_MODE}.push(\n" + ",\n".join(entries) + "\n);\n"
            f"LEVELS.{TARGET_MODE}.forEach((l, i)=>{{ if(l.name === 'CANDIDATE') l.name = 'LEVEL ' + (i + 1); }});")
    return game_patch.insert_after(source, "const LEVELS = {", BLOCK_NAME, body)


VERIFY_JS = """([mode, start])=>{
  const errors = [];
  const grid = (typeof renderLevelGrid === 'function') ? (renderLevelGrid(mode), document.querySelectorAll('#levelGrid > *').length) : null;
  return {count: LEVELS[mode].length, names: LEVELS[mode].slice(start).map(l=>l.name), grid,
          progress: (save.progress[mode]||[]).length};
}"""


def validate(stable_path, patched_path, candidates, out_dir, *, log=print):
    """Prove the patched build is safe and the candidates are real, solvable, coin-correct levels. Returns a report;
    a candidate that fails any check is marked rejected (with the reason) and must not be offered."""
    report = {"checks": [], "candidates": []}

    def check(name, ok, detail=""):
        report["checks"].append({"check": name, "ok": bool(ok), "detail": detail})
        log(f"  [{'ok' if ok else 'FAIL'}] {name}{': ' + detail if detail else ''}")
        return ok

    with cg.GameSession(stable_path) as stable:
        before = stable.page.evaluate(LEVEL_JSON_JS)
    with cg.GameSession(patched_path) as patched:
        after = patched.page.evaluate(LEVEL_JSON_JS)
        check("patched build loads without JavaScript errors", not patched.errors, "; ".join(patched.errors[:2]))
        unchanged = all(after.get(mode, [])[:len(levels)] == levels for mode, levels in before.items())
        added = {mode: len(after.get(mode, [])) - len(levels) for mode, levels in before.items()}
        check("every existing level is unchanged (all modes, same order and content)", unchanged)
        check(f"only {TARGET_MODE} gained levels, exactly the candidates",
              added.get(TARGET_MODE) == len(candidates) and all(v == 0 for m, v in added.items() if m != TARGET_MODE),
              json.dumps(added))
        first_new = len(before[TARGET_MODE])
        info = patched.page.evaluate(VERIFY_JS, [TARGET_MODE, first_new])
        check("the level grid renders every level including the candidates", info["grid"] == info["count"],
              f"{info['grid']} cards for {info['count']} levels")
        check("the save has a progress slot for each new level", info["progress"] == info["count"])

    for offset, candidate in enumerate(candidates):
        index = first_new + offset
        scene = {"mode": TARGET_MODE, "index": index, "id": cg.scene_id(TARGET_MODE, index)}
        entry = {"scene": scene["id"], "name": info["names"][offset] if offset < len(info["names"]) else None, "problems": []}
        with cg.GameSession(patched_path) as session:
            full = session.page.evaluate(cg.SOLVE_JS, [TARGET_MODE, index, True])
            basic = session.page.evaluate(cg.SOLVE_JS, [TARGET_MODE, index, False])
            if full["count"] != 1:
                entry["problems"].append(f"the game's solver finds {full['count']} coin solutions, not exactly 1")
            if full["best"] and full["best"]["rots"] != candidate["target"]:
                entry["problems"].append("the game's solver disagrees with the generator about the solution")
            if full["startSolved"] or basic["startSolved"]:
                entry["problems"].append("the level is solved at its start position")
            if basic["count"] - full["count"] > 3:
                entry["problems"].append(f"{basic['count'] - full['count']} no-coin solutions (more than 3)")
            solution = session.solve(TARGET_MODE, index)
            if not solution or not solution["bonus"]:
                entry["problems"].append("the capture solver found no coin route")
            else:
                session.start_level(TARGET_MODE, index)
                session.page.wait_for_timeout(500)
                session.begin_scene(cg.SCENE_TIMEOUT_S)
                won = session.play(solution)
                session.page.wait_for_timeout(900)
                coin = session.page.evaluate("([m,i])=>!!(save.progress[m][i] && save.progress[m][i].coin)", [TARGET_MODE, index])
                if not won:
                    entry["problems"].append("a real keyboard playthrough of the solution did not win the level")
                if not coin:
                    entry["problems"].append("the playthrough won but the game did not record the coin")
                entry["playthrough"] = {"won": won, "coin": coin, "moves": session.state()["moves"]}
            if session.errors:
                entry["problems"].append("JavaScript errors while playing: " + session.errors[0][:120])

        if not entry["problems"]:
            with cg.GameSession(patched_path) as session:
                labels = next(s["labels"] for s in session.scenes() if s["id"] == scene["id"])
                full_scene = dict(scene, labels=labels)
                try:
                    shots = cg.capture_scene(session, full_scene, out_dir, kinds=("screenshot",), frame_board=True,
                                             log=lambda *a: None)
                    entry["previews"] = [str(s["path"]) for s in shots]
                except cg.CaptureError as error:
                    entry["problems"].append(f"preview capture failed: {error}")
        entry["ok"] = not entry["problems"]
        check(f"candidate {scene['id']} is solvable, unique, coin-correct and playable", entry["ok"], "; ".join(entry["problems"]))
        report["candidates"].append(entry)

    report["ok"] = all(c["ok"] for c in report["checks"])
    return report


def pr_body(candidates, report, game, previews):
    lines = [
        "Candidate levels generated and verified automatically by "
        "[ringshift-social-autopilot](https://github.com/ultramegaray0-lgtm/ringshift-social-autopilot) `scripts/level_lab.py`.",
        "",
        "**Nothing here is live.** The demo publishes only `main`; this branch is a proposal to play and review. "
        f"Base: `{game['file']}` at `{str(game.get('commit') or '?')[:10]}`.",
        "",
        "### Checks run on this exact build",
        "",
    ]
    for check in report["checks"]:
        lines.append(f"- {'✅' if check['ok'] else '❌'} {check['check']}" + (f" ({check['detail']})" if check['detail'] else ""))
    lines.append("")
    for candidate, entry in zip(candidates, report["candidates"]):
        kinds = " + ".join(CONCEPT_NAMES.get(k, k) for k in candidate["routeKinds"])
        lines += [
            f"### {entry.get('name')} (`{entry['scene']}`): {kinds}",
            "",
            f"- Exactly one coin solution `{candidate['target']}` out of {candidate['rotations']:,} rotations; "
            f"{candidate['requiredOnlySolutions']} no-coin solution(s)",
            f"- Shortest solution {candidate['cost']} moves; {candidate['nearMisses']} near-miss boards",
            "- On the route: " + ", ".join(f"ring {g['ring']} {CONCEPT_NAMES.get(g['kind'], g['kind'])}" for g in candidate["route"]),
            f"- Real keyboard playthrough: won={entry.get('playthrough', {}).get('won')}, coin recorded={entry.get('playthrough', {}).get('coin')}",
        ]
        for url in previews.get(entry["scene"], []):
            lines.append(f"\n![{entry['scene']} preview]({url})")
        lines.append("")
    lines += [
        "### Before merging",
        "- Play each candidate. The tools prove it is solvable, unique and coin-correct; they do not prove it is fun.",
        "- Candidates are appended to Hard, so they also count toward totals that depend on the number of levels or coins.",
        "- Merging publishes the demo (the Pages workflow deploys `main`). Closing this PR discards the candidates.",
    ]
    return "\n".join(lines)


def run(args, log=print):
    game = game_source.describe(args.game_dir)
    out = Path(args.out)
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)
    today = utc_now().strftime("%Y-%m-%d")
    log(f"Game: {game['file']} at {game['commit']} (build {game['build']})")

    candidates = generate(game["path"], count=args.count, budget_s=args.budget, seed=args.seed, log=log)
    if not candidates:
        raise SystemExit("NO CANDIDATE: no generated layout passed the uniqueness, route and distinctness rules this time.")

    source = Path(game["path"]).read_text(encoding="utf-8")
    patched_path = out / game["file"]
    patched_path.write_text(patch_source(source, candidates, build=game["build"], today=today), encoding="utf-8")

    log("Validating the patched build ...")
    report = validate(game["path"], patched_path, candidates, out / "previews", log=log)
    keep = [c for c, entry in zip(candidates, report["candidates"]) if entry["ok"]]
    if not report["ok"] or not keep:
        (out / "report.json").write_text(json.dumps({"report": report, "candidates": candidates}, indent=2), encoding="utf-8")
        raise SystemExit("REJECTED: the patched build or every candidate failed validation; nothing is offered (see report.json).")
    if len(keep) != len(candidates):
        log(f"Re-patching with the {len(keep)} candidate(s) that passed, then validating again.")
        candidates = keep
        patched_path.write_text(patch_source(source, candidates, build=game["build"], today=today), encoding="utf-8")
        report = validate(game["path"], patched_path, candidates, out / "previews", log=log)
        if not report["ok"] or not all(entry["ok"] for entry in report["candidates"]):
            raise SystemExit("REJECTED on re-validation; nothing is offered.")

    (out / "report.json").write_text(json.dumps({"game": game, "report": report, "candidates": candidates}, indent=2), encoding="utf-8")
    log(f"VALIDATED: {len(candidates)} candidate level(s) -> {patched_path}")

    if not args.open_pr:
        return {"pr": None, "report": report}

    game_repo.require_write_access()
    previews = game_repo.host_previews(report, label="levels")
    body = pr_body(candidates, report, game, previews)
    (out / "pr_body.md").write_text(body, encoding="utf-8")
    pr = game_repo.propose(patched_path, game, branch_prefix="lab/levels", title=f"Level lab: {len(candidates)} candidate level(s) ({today})",
                           body=body, log=log)
    return {"pr": pr, "report": report}


def main(argv=None):
    parser = argparse.ArgumentParser(description="Generate, verify and propose candidate Ringshift levels.")
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("run", help="generate candidates, patch a copy of the stable build, validate it")
    p.add_argument("--game-dir", default="game-src")
    p.add_argument("--out", default="lab-out")
    p.add_argument("--count", type=int, default=DEFAULT_COUNT)
    p.add_argument("--budget", type=int, default=DEFAULT_BUDGET_S, help="seconds of generation (default 240)")
    p.add_argument("--seed", type=int)
    p.add_argument("--open-pr", action="store_true", help="push a branch to the game repository and open a pull request")
    p.add_argument("--no-record", action="store_true", help="do not write data/game_lab.json")
    args = parser.parse_args(argv)
    entry = {"at": utc_now().isoformat(timespec="seconds"), "outcome": "error"}
    try:
        result = run(args)
    except (game_source.GameSourceError, cg.CaptureError, game_patch.PatchError, game_repo.GameRepoError) as error:
        print(f"error: {error}", file=sys.stderr)
        entry["error"] = str(error)[:400]
        _record(entry, args)
        _summary(f"### Level lab\n\nError: {error}\n")
        return 2
    except SystemExit as stop:          # NO CANDIDATE / REJECTED: an honest outcome, recorded, not a crash
        entry.update(outcome="no candidate", error=str(stop.code)[:400])
        _record(entry, args)
        _summary(f"### Level lab\n\n{stop.code}\n")
        print(stop.code)
        return 0
    report = result.get("report") or {}
    entry.update(outcome="proposed" if result.get("pr") else "validated", pr=result.get("pr"),
                 candidates=[{"scene": c["scene"], "name": c.get("name"), "playthrough": c.get("playthrough")}
                             for c in report.get("candidates", [])])
    _record(entry, args)
    if result.get("pr"):
        print(f"PULL REQUEST: {result['pr']}")
        _summary(f"### Level lab\n\nCandidate levels validated and proposed: {result['pr']}\n")
    else:
        _summary("### Level lab\n\nCandidates validated (no pull request requested).\n")
    return 0


def _record(entry, args):
    if getattr(args, "no_record", False):
        return
    path = BASE_DIR / "data" / "game_lab.json"
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        state = {}
    if not isinstance(state, dict):
        state = {}
    state.setdefault("themes", [])
    state["levels"] = (state.get("levels") or []) + [entry]
    state["levels"] = state["levels"][-100:]
    path.write_text(json.dumps(state, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def _summary(text):
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if path:
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(text + "\n")


if __name__ == "__main__":
    raise SystemExit(main())
