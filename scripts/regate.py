"""Re-run the AI review gate on existing rendered clips (no re-render).

Usage: DATABASE_URL=... LLM_API_KEY=... python scripts/regate.py <campaign> [--only-failed]

Re-judges clips in place and updates gate_status/gate_reasons/formula_score.
Use after a gate-logic fix so already-rendered (paid-for) clips get a fair
verdict instead of being re-rendered.
"""

from __future__ import annotations

import sys


def main() -> None:
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(2)
    campaign = sys.argv[1]
    only_failed = "--only-failed" in sys.argv

    from core.config import load_campaign
    from core.db import get_session
    from core.models import Clip, Transcript
    from producer.review_gate import run_gate

    cfg = load_campaign(f"campaigns/{campaign}.yaml", strict_assets=False)

    with get_session() as session:
        q = session.query(Clip).filter(Clip.campaign == campaign, Clip.kind == "clip")
        if only_failed:
            q = q.filter(Clip.gate_status.in_(["didnt_pass", "pending"]))
        clips = q.order_by(Clip.id).all()
        print(f"{len(clips)} clip(s) to re-gate for {campaign!r}")

        for clip in clips:
            segments = None
            if clip.source_id:
                t = session.query(Transcript).filter_by(source_id=clip.source_id).first()
                segments = t.segments if t else None

            result = run_gate(clip, clip.file_path or "", segments, cfg, session)
            clip.gate_status = result.gate_status
            clip.gate_reasons = result.gate_reasons
            clip.formula_score = result.formula_score
            session.commit()
            fails = [
                f"{r.get('check')}"
                for r in (result.gate_reasons or [])
                if isinstance(r, dict) and not r.get("pass", True)
            ]
            print(
                f"clip {clip.id}: {result.gate_status}"
                f" formula={result.formula_score}"
                + (f" fails={fails}" if fails else "")
            )


if __name__ == "__main__":
    main()
