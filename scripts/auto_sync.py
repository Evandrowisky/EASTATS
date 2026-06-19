import argparse
import json
import os
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def main():
    parser = argparse.ArgumentParser(description="Sincroniza clubes do ClubScout Pro fora da Vercel.")
    parser.add_argument("--limit", type=int, default=int(os.getenv("AUTO_SYNC_LIMIT", "25")))
    args = parser.parse_args()

    os.environ.setdefault("USE_SUPABASE", "1")

    import main as app_main

    if not app_main.get_supabase():
        raise SystemExit("Supabase nao configurado. Defina SUPABASE_URL e SUPABASE_SERVICE_ROLE_KEY nos Secrets.")

    clubs = app_main.load_admin_clubs_for_cron(limit=args.limit)
    if not clubs:
        print("[AUTO_SYNC] Nenhum clube com admin ativo encontrado.")
        return 0

    results = []
    for club in clubs:
        try:
            result = app_main.sync_club_for_auto_update(club)
            results.append(result)
            print(
                "[AUTO_SYNC] OK "
                f"{result.get('club')} ({result.get('club_id')}): "
                f"novas={result.get('new_matches')} total={result.get('total_matches')}"
            )
        except Exception as exc:
            club_id = str(club.get("club_id") or "")
            error = f"{type(exc).__name__}: {exc}"
            print(f"[AUTO_SYNC] ERRO {club.get('name') or club_id}: {error}")
            try:
                app_main.log_sync_supabase(
                    club_id=club_id,
                    platform=club.get("platform") or "common-gen5",
                    status="error",
                    total_matches=0,
                    new_matches=0,
                    message=f"Erro no GitHub Actions: {error}",
                    debug={"club": club},
                )
            except Exception:
                pass
            results.append({"club_id": club_id, "success": False, "error": error})

    print("[AUTO_SYNC] RESUMO")
    print(json.dumps(results, ensure_ascii=False, indent=2, default=str))

    failed = [r for r in results if not r.get("success")]
    return 1 if failed and len(failed) == len(results) else 0


if __name__ == "__main__":
    raise SystemExit(main())
