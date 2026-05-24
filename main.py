# -*- coding: utf-8 -*-
"""
Scout Clubs Pro v2 - Análise Profissional EA FC
Inspirado no app Scout Clubs original
- Abas: Visão, Jogadores, Comparar, Confrontos, Time Ideal, Agenda
- Formação tática visual com mapinha do campo
- MOM (Melhor da Partida) por jogo
- Gráficos circulares e de barras
- Cache JSON + sincronização com progresso em tempo real
"""

import os
import json
import sqlite3
import asyncio
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional, Dict, List, Any
from contextlib import asynccontextmanager
from statistics import mean, pstdev

from fastapi import FastAPI, HTTPException, Query, Depends, Header
from pydantic import BaseModel
from typing import Optional
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.middleware.cors import CORSMiddleware

# ============================================================
# CONFIGURAÇÃO
# ============================================================

APP_NAME = "Scout Clubs Pro"

# Vercel só permite escrita temporária em /tmp
if os.getenv("VERCEL") == "1":
    DB_FILE = "/tmp/scout_clubs.db"
    JSON_CACHE = "/tmp/dados_clube.json"
    CLUB_JSON_DIR = "/tmp/dados_clubes"
else:
    DB_FILE = "scout_clubs.db"
    JSON_CACHE = "dados_clube.json"
    CLUB_JSON_DIR = "dados_clubes"

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
JWT_ALGORITHM = "HS256"
JWT_EXPIRE_DAYS = int(os.getenv("JWT_EXPIRE_DAYS", "7") or "7")

MATCH_TYPE_CANDIDATES = [
    "leagueMatch",
    "playoffMatch",
    "friendlyMatch",
    "friendlies",
    "friendly",
    "clubFriendly",
    "practiceMatch",
    "gameType9",
    "gameType13",
    "gameType15",
    "gameType16",
]

# ============================================================
# CLIENTE EA FC
# ============================================================

class EAFCClient:
    """Cliente para API do EA FC"""
    
    BASE_URL = "https://proclubs.ea.com/api/fc"
    
    def __init__(self):
        self.using_curl_cffi = False
        try:
            from curl_cffi import requests as cffi_requests
            # tenta varios browsers ate funcionar
            for impersonate in ["chrome120", "chrome110", "chrome116", "firefox133", "safari17_0"]:
                try:
                    self.session = cffi_requests.Session(impersonate=impersonate)
                    self.using_curl_cffi = True
                    print(f"[EA FC] Usando curl_cffi com {impersonate}")
                    break
                except Exception:
                    continue
            if not self.using_curl_cffi:
                raise ImportError
        except ImportError:
            import requests
            self.session = requests.Session()
            print("[EA FC] Usando requests simples (curl_cffi nao disponivel)")
        
        # Headers que funcionam com a API EA FC (testado em 2026)
        self.session.headers.update({
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:109.0) Gecko/20100101 Firefox/112.0",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
            "Accept-Encoding": "gzip, deflate, br",
            "Accept-Language": "pt-BR,pt;q=0.9,en;q=0.8",
            "Referer": "https://www.ea.com/",
            "Origin": "https://www.ea.com",
        })
    
    def _get(self, url: str, params: dict = None, timeout: int = 30):
        try:
            r = self.session.get(url, params=params, timeout=timeout)
            print(f"[EA FC] GET {url} {params} -> {r.status_code} ({len(r.text)} bytes)")
            if r.status_code == 200 and r.text:
                # mostra preview do retorno
                preview = r.text[:200].replace('\n', ' ')
                print(f"[EA FC]   preview: {preview}")
                try:
                    return r.json()
                except Exception as je:
                    print(f"[EA FC]   nao eh json: {je}")
                    return {}
            else:
                print(f"[EA FC]   body: {r.text[:300]}")
            return {}
        except Exception as e:
            print(f"[EA FC] Erro de conexao em {url}: {type(e).__name__}: {e}")
            return {}
    
    def search_club(self, club_name: str, platform: str = "common-gen5"):
        # Endpoint correto (EA mudou em 2025): allTimeLeaderboard/search
        url = f"{self.BASE_URL}/allTimeLeaderboard/search"
        # Tenta variacoes do nome e multiplas plataformas
        name_variants = [
            club_name,
            club_name.upper(),
            club_name.title(),
            club_name.lower(),
            club_name.replace(' SC', '').strip(),
            club_name.replace('SC', '').strip(),
        ]
        if platform == "auto" or not platform:
            platforms = ["common-gen5", "common-gen4"]
        else:
            platforms = [platform]
        seen = set()
        for plat in platforms:
            for nm in name_variants:
                if not nm or (plat, nm) in seen:
                    continue
                seen.add((plat, nm))
                params = {"clubName": nm, "platform": plat}
                result = self._get(url, params)
                if result and isinstance(result, list) and len(result) > 0:
                    club = result[0]
                    # Nome esta em clubName ou clubInfo.name
                    cname = club.get("clubName") or club.get("name") or (
                        club.get("clubInfo", {}).get("name") if isinstance(club.get("clubInfo"), dict) else None
                    ) or nm
                    return {
                        "success": True,
                        "clubId": str(club.get("clubId")),
                        "name": cname,
                        "platform": plat,
                        "raw": club,
                    }
        return {"success": False}
    
    def club_info(self, club_id: str, platform: str = "common-gen5"):
        url = f"{self.BASE_URL}/clubs/info"
        params = {"clubIds": club_id, "platform": platform}
        return self._get(url, params)
    
    def overall_stats(self, club_id: str, platform: str = "common-gen5"):
        url = f"{self.BASE_URL}/clubs/overallStats"
        params = {"clubIds": club_id, "platform": platform}
        return self._get(url, params)
    
    def members(self, club_id: str, platform: str = "common-gen5"):
        url = f"{self.BASE_URL}/members/career/stats"
        params = {"clubId": club_id, "platform": platform}
        return self._get(url, params)
    
    def matches(self, club_id: str, match_type: str = "leagueMatch", platform: str = "common-gen5", max_count: int = 100):
        url = f"{self.BASE_URL}/clubs/matches"
        params = {
            "clubIds": club_id,
            "platform": platform,
            "matchType": match_type,
            "maxResultCount": max_count,
        }
        return self._get(url, params)


# ============================================================
# BANCO DE DADOS
# ============================================================

def init_db():
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.executescript("""
        CREATE TABLE IF NOT EXISTS clubs (
            club_id TEXT PRIMARY KEY,
            name TEXT,
            platform TEXT,
            data TEXT,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS players (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            club_id TEXT,
            name TEXT,
            position TEXT,
            data TEXT,
            UNIQUE(club_id, name)
        );
        CREATE TABLE IF NOT EXISTS matches (
            match_id TEXT PRIMARY KEY,
            club_id TEXT,
            opponent TEXT,
            score TEXT,
            result TEXT,
            match_type TEXT,
            timestamp INTEGER,
            data TEXT
        );
        CREATE TABLE IF NOT EXISTS player_profiles (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            club_id TEXT NOT NULL,
            player_name TEXT NOT NULL,
            manual_position TEXT,
            archetype TEXT,
            playstyles TEXT,
            notes TEXT,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(club_id, player_name)
        );
        CREATE TABLE IF NOT EXISTS agenda (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            opponent TEXT NOT NULL,
            match_date TEXT NOT NULL,
            match_time TEXT,
            match_type TEXT DEFAULT 'liga',
            location TEXT,
            notes TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
    """)
    # Auto-migra: se ja existe DB antigo sem a coluna 'data', adiciona
    for table in ("clubs", "players", "matches"):
        try:
            c.execute(f"PRAGMA table_info({table})")
            cols = [row[1] for row in c.fetchall()]
            if cols and "data" not in cols:
                c.execute(f"ALTER TABLE {table} ADD COLUMN data TEXT")
                print(f"[DB] Migracao: coluna 'data' adicionada em '{table}'")
        except Exception as e:
            print(f"[DB] Aviso ao migrar {table}: {e}")
    try:
        c.execute("PRAGMA table_info(player_profiles)")
        profile_cols = [row[1] for row in c.fetchall()]
        if profile_cols and "playstyles" not in profile_cols:
            c.execute("ALTER TABLE player_profiles ADD COLUMN playstyles TEXT")
            print("[DB] Migracao: coluna 'playstyles' adicionada em 'player_profiles'")
    except Exception as e:
        print(f"[DB] Aviso ao migrar player_profiles: {e}")
    conn.commit()
    conn.close()


# ============================================================
# CACHE JSON
# ============================================================

def save_cache(data: dict):
    with open(JSON_CACHE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2, default=str)
    print(f"[CACHE] Dados salvos em {JSON_CACHE}")

def load_cache() -> Optional[dict]:
    if not Path(JSON_CACHE).exists():
        return None
    try:
        with open(JSON_CACHE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None





def _safe_json_name(value: str) -> str:
    clean = "".join(ch if ch.isalnum() or ch in ("-", "_") else "_" for ch in str(value or "club"))
    return clean.strip("_") or "club"


def club_json_path(club_id: str) -> Path:
    folder = Path(CLUB_JSON_DIR)
    folder.mkdir(parents=True, exist_ok=True)
    return folder / f"{_safe_json_name(club_id)}.json"


def load_club_json_history(club_id: str) -> Optional[dict]:
    try:
        path = club_json_path(club_id)
        if not path.exists():
            return None
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        print(f"[CLUB JSON] Aviso ao ler historico do clube {club_id}: {e}")
        return None


def save_club_json_history(club_id: str, data: dict):
    try:
        path = club_json_path(club_id)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2, default=str)
        print(f"[CLUB JSON] Historico do clube salvo em {path}")
    except Exception as e:
        print(f"[CLUB JSON] Aviso ao salvar historico do clube {club_id}: {e}")

def _norm_club_name(value: str) -> str:
    return " ".join(str(value or "").strip().lower().split())


def _club_name_matches(wanted: str, candidate: str) -> bool:
    wanted = _norm_club_name(wanted)
    candidate = _norm_club_name(candidate)
    if not wanted or not candidate:
        return False
    return wanted == candidate or wanted in candidate or candidate in wanted


def fallback_search_from_local(club_name: str = "", platform: str = "auto") -> Optional[dict]:
    """Usa cache/SQLite somente quando o nome pedido bate com o clube salvo."""
    wanted = _norm_club_name(club_name)

    cache = load_cache() or {}
    club = cache.get("club") or {}
    if club.get("id"):
        cached_name = str(club.get("name") or "")
        if not wanted or _club_name_matches(wanted, cached_name):
            return {
                "success": True,
                "clubId": str(club.get("id")),
                "name": cached_name or club_name or "Clube em cache",
                "platform": club.get("platform") or (platform if platform != "auto" else "common-gen5"),
                "source": "cache",
            }

    try:
        conn = sqlite3.connect(DB_FILE)
        conn.row_factory = sqlite3.Row
        row = None
        if wanted:
            row = conn.execute(
                "SELECT club_id, name, platform FROM clubs WHERE lower(name)=? ORDER BY updated_at DESC LIMIT 1",
                (wanted,),
            ).fetchone()
            if row is None:
                row = conn.execute(
                    "SELECT club_id, name, platform FROM clubs WHERE lower(name) LIKE ? ORDER BY updated_at DESC LIMIT 1",
                    (f"%{wanted}%",),
                ).fetchone()
        elif not wanted:
            row = conn.execute("SELECT club_id, name, platform FROM clubs ORDER BY updated_at DESC LIMIT 1").fetchone()
        conn.close()
        if row and (not wanted or _club_name_matches(wanted, row["name"])):
            return {
                "success": True,
                "clubId": str(row["club_id"]),
                "name": row["name"] or club_name or "Clube salvo",
                "platform": row["platform"] or (platform if platform != "auto" else "common-gen5"),
                "source": "sqlite",
            }
    except Exception as e:
        print(f"[EA FC] Fallback local de clube falhou: {e}")

    return None


# ============================================================
# PROCESSAMENTO DE DADOS
# ============================================================

def calc_club_stats(overall_data, club_info_data, matches_list):
    """Calcula estatísticas agregadas do clube"""
    stats = {
        "win_rate": 0, "goals_for": 0, "goals_against": 0, "goal_diff": 0,
        "wins": 0, "draws": 0, "losses": 0, "matches_played": 0,
        "goals_per_match": 0, "shots_per_match": 0, "clean_sheets": 0,
        "pass_pct": 0, "tackle_pct": 0, "best_streak": 0, "shooting": 0
    }
    
    if isinstance(overall_data, list) and overall_data:
        d = overall_data[0]
        stats["wins"] = int(d.get("wins", 0))
        stats["draws"] = int(d.get("ties", 0))
        stats["losses"] = int(d.get("losses", 0))
        stats["goals_for"] = int(d.get("goals", 0))
        stats["goals_against"] = int(d.get("goalsAgainst", 0))
        stats["matches_played"] = stats["wins"] + stats["draws"] + stats["losses"]
        if stats["matches_played"] > 0:
            stats["win_rate"] = round((stats["wins"] / stats["matches_played"]) * 100)
            stats["goals_per_match"] = round(stats["goals_for"] / stats["matches_played"], 1)
        stats["goal_diff"] = stats["goals_for"] - stats["goals_against"]
    
    # Calcula melhor sequência e clean sheets das partidas
    if matches_list:
        streak = current_streak = 0
        for m in matches_list:
            if m.get("result") == "V":
                current_streak += 1
                streak = max(streak, current_streak)
                if m.get("goals_against", 0) == 0:
                    stats["clean_sheets"] += 1
            else:
                current_streak = 0
        stats["best_streak"] = streak
    
    return stats


def fetch_all_match_types(ea_client, club_id, platform, max_count=100):
    """Testa matchTypes e varios maxResultCount, porque a API da EA as vezes devolve menos com 100 do que com 20/50."""
    all_matches_raw = []
    seen = set()
    debug = []
    request_counts = []
    for n in (max_count, 100, 50, 20):
        try:
            n = int(n)
        except Exception:
            continue
        if n > 0 and n not in request_counts:
            request_counts.append(n)

    for match_type in MATCH_TYPE_CANDIDATES:
        entry = {
            "matchType": match_type,
            "requested": request_counts,
            "ok": False,
            "count": 0,
            "unique_added": 0,
            "duplicates": 0,
            "attempts": [],
            "error": None,
        }
        for requested_count in request_counts:
            attempt = {"requested": requested_count, "ok": False, "count": 0, "unique_added": 0, "duplicates": 0, "error": None}
            try:
                raw = ea_client.matches(club_id, match_type, platform, max_count=requested_count)
                if isinstance(raw, list):
                    attempt["ok"] = True
                    entry["ok"] = True
                    attempt["count"] = len(raw)
                    entry["count"] += len(raw)
                    for idx, m in enumerate(raw):
                        if not isinstance(m, dict):
                            continue
                        match_id = str(m.get("matchId") or "").strip()
                        if not match_id:
                            match_id = f"{match_type}:{m.get('timestamp', '')}:{idx}:{json.dumps(m.get('clubs', {}), sort_keys=True)}"
                        if match_id in seen:
                            attempt["duplicates"] += 1
                            entry["duplicates"] += 1
                            continue
                        seen.add(match_id)
                        enriched = dict(m)
                        enriched["_origin"] = match_type
                        enriched["_requested_count"] = requested_count
                        all_matches_raw.append(enriched)
                        attempt["unique_added"] += 1
                        entry["unique_added"] += 1
                else:
                    attempt["error"] = f"Retorno inesperado: {type(raw).__name__}"
            except Exception as e:
                attempt["error"] = f"{type(e).__name__}: {e}"
            entry["attempts"].append(attempt)

        errors = [a["error"] for a in entry["attempts"] if a.get("error")]
        if errors and not entry["ok"]:
            entry["error"] = " | ".join(errors[:3])

        attempts_msg = ", ".join(
            f"{a['requested']}=>{a['count']} (+{a['unique_added']}, dup {a['duplicates']})"
            for a in entry["attempts"]
        )
        print(
            f"[EA FC] matchType={match_type} attempts=[{attempts_msg}] "
            f"unique_added={entry['unique_added']} duplicates={entry['duplicates']} error={entry['error']}"
        )
        debug.append(entry)

    print(f"[EA FC] Total bruto unico apos dedupe: {len(all_matches_raw)}")
    return all_matches_raw, debug



def stable_match_id_for_storage(match: dict, club_id: str, index: int = 0) -> str:
    mid = str((match or {}).get("match_id") or (match or {}).get("matchId") or "").strip()
    if mid and mid.lower() not in ("none", "null", "undefined", "0"):
        return mid
    return (
        f"fallback:{club_id}:{(match or {}).get('timestamp', '')}:"
        f"{(match or {}).get('opponent', '')}:{(match or {}).get('score', '')}:"
        f"{(match or {}).get('match_type', '')}:{index}"
    )


def merge_match_lists_for_storage(club_id: str, *groups):
    merged = {}
    fallback_i = 0
    for group in groups:
        for item in group or []:
            if not isinstance(item, dict):
                continue
            mid = stable_match_id_for_storage(item, club_id, fallback_i)
            if mid.startswith("fallback:"):
                fallback_i += 1
            current = merged.get(mid, {})
            merged[mid] = {**current, **item, "match_id": mid}
    return sorted(merged.values(), key=lambda x: int(x.get("timestamp", 0) or 0), reverse=True)


# ============================================================
# SUPABASE
# ============================================================

_SUPABASE_CLIENT = None


def _env_flag(name: str, default: str = "0") -> bool:
    return str(os.getenv(name, default)).strip().lower() in ("1", "true", "yes", "on")


def _safe_int(value, default: int = 0) -> int:
    try:
        if value is None or value == "":
            return default
        return int(float(value))
    except Exception:
        return default


def _safe_float(value, default: float = 0.0) -> float:
    try:
        if value is None or value == "":
            return default
        return float(value)
    except Exception:
        return default


def _now_iso() -> str:
    return datetime.now().isoformat()


def _supabase_chunks(rows, size: int = 500):
    for i in range(0, len(rows), size):
        yield rows[i:i + size]


def get_supabase():
    """Retorna cliente Supabase backend-only, ou None se nao estiver configurado."""
    global _SUPABASE_CLIENT
    if not _env_flag("USE_SUPABASE"):
        return None
    if _SUPABASE_CLIENT is not None:
        return _SUPABASE_CLIENT

    url = os.getenv("SUPABASE_URL", "").strip()
    key = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "").strip()
    if not url or not key:
        print("[SUPABASE] USE_SUPABASE=1, mas SUPABASE_URL ou SERVICE_ROLE_KEY ausente")
        return None

    try:
        from supabase import create_client
        _SUPABASE_CLIENT = create_client(url, key)
        print("[SUPABASE] Cliente inicializado")
        return _SUPABASE_CLIENT
    except Exception as e:
        print(f"[SUPABASE] Falha ao inicializar cliente: {type(e).__name__}: {e}")
        return None


def save_club_supabase(club_data: dict):
    sb = get_supabase()
    if not sb or not club_data:
        return False
    club = club_data.get("club") or {}
    club_id = str(club.get("id") or "").strip()
    if not club_id:
        return False
    row = {
        "club_id": club_id,
        "name": club.get("name") or club_data.get("name") or "Clube",
        "platform": club.get("platform") or "common-gen5",
        "data": club_data,
        "synced_at": club.get("synced_at") or _now_iso(),
        "updated_at": _now_iso(),
    }
    try:
        sb.table("clubs").upsert(row, on_conflict="club_id").execute()
        print(f"[SUPABASE] Clube salvo: {club_id}")
        return True
    except Exception as e:
        print(f"[SUPABASE] Aviso ao salvar clube {club_id}: {type(e).__name__}: {e}")
        return False


def save_players_supabase(club_id: str, players: list):
    sb = get_supabase()
    if not sb or not club_id or not players:
        return 0
    rows = []
    for p in players or []:
        if not isinstance(p, dict) or not p.get("name"):
            continue
        rows.append({
            "club_id": str(club_id),
            "name": str(p.get("name") or ""),
            "position": p.get("position"),
            "games": _safe_int(p.get("games")),
            "rating": _safe_float(p.get("rating")),
            "goals": _safe_int(p.get("goals")),
            "assists": _safe_int(p.get("assists")),
            "shots": _safe_int(p.get("shots")),
            "passes_made": _safe_int(p.get("passes_made")),
            "pass_pct": _safe_float(p.get("pass_pct")),
            "tackles_made": _safe_int(p.get("tackles_made")),
            "tackle_pct": _safe_float(p.get("tackle_pct")),
            "mom": _safe_int(p.get("mom")),
            "goals_per_game": _safe_float(p.get("goals_per_game")),
            "assists_per_game": _safe_float(p.get("assists_per_game")),
            "data": p,
            "updated_at": _now_iso(),
        })
    saved = 0
    try:
        for chunk in _supabase_chunks(rows):
            sb.table("players").upsert(chunk, on_conflict="club_id,name").execute()
            saved += len(chunk)
        print(f"[SUPABASE] Jogadores salvos: {saved}")
    except Exception as e:
        print(f"[SUPABASE] Aviso ao salvar jogadores: {type(e).__name__}: {e}")
    return saved


def save_matches_supabase(club_id: str, matches: list):
    sb = get_supabase()
    if not sb or not club_id or not matches:
        return {"matches": 0, "match_players": 0}

    match_rows = []
    player_rows = []
    for idx, m in enumerate(matches or []):
        if not isinstance(m, dict):
            continue
        mid = stable_match_id_for_storage(m, club_id, idx)
        m_payload = {**m, "match_id": mid}
        match_rows.append({
            "match_id": mid,
            "club_id": str(club_id),
            "opponent": m.get("opponent"),
            "score": m.get("score"),
            "goals_for": _safe_int(m.get("goals_for")),
            "goals_against": _safe_int(m.get("goals_against")),
            "result": m.get("result"),
            "match_type": m.get("match_type"),
            "match_type_raw": m.get("match_type_raw") or m.get("_origin"),
            "match_timestamp": _safe_int(m.get("timestamp")),
            "match_date": m.get("date"),
            "mom": m.get("mom"),
            "mom_rating": _safe_float(m.get("mom_rating")),
            "data": m_payload,
            "updated_at": _now_iso(),
        })
        for pr in m.get("players_ratings") or []:
            if not isinstance(pr, dict) or not pr.get("name"):
                continue
            player_rows.append({
                "match_id": mid,
                "club_id": str(club_id),
                "player_name": str(pr.get("name") or ""),
                "position": pr.get("pos") or pr.get("position"),
                "rating": _safe_float(pr.get("rating")),
                "sofi_rating": _safe_float(pr.get("sofi_rating", pr.get("rating"))),
                "goals": _safe_int(pr.get("goals")),
                "assists": _safe_int(pr.get("assists")),
                "shots": _safe_int(pr.get("shots")),
                "passes_made": _safe_int(pr.get("passes_made")),
                "pass_pct": _safe_float(pr.get("pass_pct")),
                "tackles_made": _safe_int(pr.get("tackles_made")),
                "tackle_pct": _safe_float(pr.get("tackle_pct")),
                "saves": _safe_int(pr.get("saves")),
                "clean_sheet": _safe_int(pr.get("clean_sheet")),
                "red": _safe_int(pr.get("red")),
                "mom": _safe_int(pr.get("mom")),
                "data": pr,
                "updated_at": _now_iso(),
            })

    saved_matches = 0
    saved_players = 0
    try:
        for chunk in _supabase_chunks(match_rows):
            sb.table("matches").upsert(chunk, on_conflict="match_id").execute()
            saved_matches += len(chunk)
        for chunk in _supabase_chunks(player_rows):
            sb.table("match_players").upsert(chunk, on_conflict="match_id,player_name").execute()
            saved_players += len(chunk)
        print(f"[SUPABASE] Partidas salvas: {saved_matches}; atuações: {saved_players}")
    except Exception as e:
        print(f"[SUPABASE] Aviso ao salvar partidas/atuações: {type(e).__name__}: {e}")
    return {"matches": saved_matches, "match_players": saved_players}


def load_matches_supabase(club_id: str, limit: int = 1000):
    sb = get_supabase()
    if not sb or not club_id:
        return []
    try:
        resp = (
            sb.table("matches")
            .select("data")
            .eq("club_id", str(club_id))
            .order("match_timestamp", desc=True)
            .limit(int(limit))
            .execute()
        )
        rows = getattr(resp, "data", None) or []
        out = []
        for row in rows:
            data = row.get("data") if isinstance(row, dict) else None
            if isinstance(data, dict):
                out.append(data)
        print(f"[SUPABASE] Histórico carregado: {len(out)} partidas")
        return out
    except Exception as e:
        print(f"[SUPABASE] Aviso ao carregar partidas: {type(e).__name__}: {e}")
        return []


def log_sync_supabase(club_id: str, platform: str, status: str, total_matches: int = 0, new_matches: int = 0, message: str = "", debug: Any = None):
    sb = get_supabase()
    if not sb:
        return False
    try:
        sb.table("sync_logs").insert({
            "club_id": str(club_id) if club_id else None,
            "platform": platform,
            "status": status,
            "total_matches": _safe_int(total_matches),
            "new_matches": _safe_int(new_matches),
            "message": message,
            "debug": debug,
        }).execute()
        return True
    except Exception as e:
        print(f"[SUPABASE] Aviso ao registrar sync_log: {type(e).__name__}: {e}")
        return False


def load_latest_club_data_supabase(club_id: Optional[str] = None):
    sb = get_supabase()
    if not sb:
        return None
    try:
        q = sb.table("clubs").select("data,synced_at,updated_at")
        if club_id:
            q = q.eq("club_id", str(club_id))
        resp = q.order("synced_at", desc=True).limit(1).execute()
        rows = getattr(resp, "data", None) or []
        if rows and isinstance(rows[0], dict) and isinstance(rows[0].get("data"), dict):
            print("[SUPABASE] Dashboard carregado de clubs.data")
            return rows[0]["data"]
    except Exception as e:
        print(f"[SUPABASE] Aviso ao carregar club_data: {type(e).__name__}: {e}")
    return None


def load_player_profiles_supabase(club_id: str) -> Dict[str, Dict[str, Any]]:
    sb = get_supabase()
    if not sb or not club_id:
        return {}
    try:
        resp = sb.table("player_profiles").select("player_name,manual_position,archetype,playstyles,notes").eq("club_id", str(club_id)).execute()
        rows = getattr(resp, "data", None) or []
        return {
            r.get("player_name"): {
                "manual_position": r.get("manual_position"),
                "archetype": r.get("archetype"),
                "playstyles": r.get("playstyles") or [],
                "notes": r.get("notes"),
            }
            for r in rows if isinstance(r, dict) and r.get("player_name")
        }
    except Exception as e:
        print(f"[SUPABASE] Aviso ao carregar player_profiles: {type(e).__name__}: {e}")
        return {}


def save_player_profile_supabase(club_id: str, player_name: str, manual_position=None, archetype=None, playstyles=None, notes=None):
    sb = get_supabase()
    if not sb or not club_id or not player_name:
        return False
    try:
        if manual_position or archetype or playstyles or notes:
            sb.table("player_profiles").upsert({
                "club_id": str(club_id),
                "player_name": str(player_name),
                "manual_position": manual_position,
                "archetype": archetype,
                "playstyles": playstyles or [],
                "notes": notes,
                "updated_at": _now_iso(),
            }, on_conflict="club_id,player_name").execute()
        else:
            sb.table("player_profiles").delete().eq("club_id", str(club_id)).eq("player_name", str(player_name)).execute()
        return True
    except Exception as e:
        print(f"[SUPABASE] Aviso ao salvar player_profile {player_name}: {type(e).__name__}: {e}")
        return False

# ============================================================
# AUTENTICACAO / APP USERS
# ============================================================

_pwd_context = None


class AuthRegisterPayload(BaseModel):
    nome: str
    usuario: str
    senha: str
    clube: str


class AuthLoginPayload(BaseModel):
    usuario: str
    senha: str


def _get_pwd_context():
    global _pwd_context
    if _pwd_context is None:
        try:
            from passlib.context import CryptContext
            _pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")
        except Exception as e:
            print(f"[AUTH] passlib/bcrypt indisponivel: {type(e).__name__}: {e}")
            raise HTTPException(500, "Dependencia de autenticacao ausente: passlib[bcrypt]")
    return _pwd_context


def hash_password(senha: str) -> str:
    return _get_pwd_context().hash(str(senha or ""))


def verify_password(senha: str, password_hash: str) -> bool:
    try:
        return _get_pwd_context().verify(str(senha or ""), str(password_hash or ""))
    except Exception:
        return False


def _jwt_secret() -> str:
    secret = os.getenv("JWT_SECRET_KEY", "").strip()
    if not secret:
        print("[AUTH] JWT_SECRET_KEY ausente")
        raise HTTPException(500, "JWT_SECRET_KEY nao configurada no servidor")
    return secret


def create_access_token(data: dict):
    try:
        from jose import jwt
    except Exception as e:
        print(f"[AUTH] python-jose indisponivel: {type(e).__name__}: {e}")
        raise HTTPException(500, "Dependencia de autenticacao ausente: python-jose[cryptography]")
    payload = dict(data or {})
    expire = datetime.now(timezone.utc) + timedelta(days=JWT_EXPIRE_DAYS)
    payload.update({"exp": expire})
    return jwt.encode(payload, _jwt_secret(), algorithm=JWT_ALGORITHM)


def decode_access_token(token: str):
    try:
        from jose import JWTError, jwt
        return jwt.decode(str(token or ""), _jwt_secret(), algorithms=[JWT_ALGORITHM])
    except Exception as e:
        print(f"[AUTH] Token invalido: {type(e).__name__}: {e}")
        raise HTTPException(401, "Token invalido ou expirado")


def _public_user(row: dict) -> dict:
    if not isinstance(row, dict):
        return {}
    return {
        "id": str(row.get("id") or ""),
        "nome": row.get("nome") or "",
        "usuario": row.get("usuario") or "",
        "club_id": str(row.get("club_id") or ""),
        "clube": row.get("clube") or "",
        "cargo": row.get("cargo") or "jogador",
        "status": row.get("status") or "ativo",
        "is_active": bool(row.get("is_active", True)),
    }


def _get_app_user_by_usuario(usuario: str):
    sb = get_supabase()
    if not sb:
        raise HTTPException(503, "Supabase nao configurado para autenticacao")
    try:
        resp = sb.table("app_users").select("*").eq("usuario", str(usuario or "").strip().lower()).limit(1).execute()
        rows = getattr(resp, "data", None) or []
        return rows[0] if rows else None
    except Exception as e:
        print(f"[AUTH] Erro buscando usuario: {type(e).__name__}: {e}")
        raise HTTPException(500, "Erro ao consultar usuario")


def _get_app_user_by_id(user_id: str):
    sb = get_supabase()
    if not sb:
        raise HTTPException(503, "Supabase nao configurado para autenticacao")
    try:
        resp = sb.table("app_users").select("*").eq("id", str(user_id or "")).limit(1).execute()
        rows = getattr(resp, "data", None) or []
        return rows[0] if rows else None
    except Exception as e:
        print(f"[AUTH] Erro buscando usuario por id: {type(e).__name__}: {e}")
        raise HTTPException(500, "Erro ao consultar usuario")


def club_has_admin(club_id: str) -> bool:
    sb = get_supabase()
    if not sb or not club_id:
        return False
    try:
        resp = (
            sb.table("app_users")
            .select("id")
            .eq("club_id", str(club_id))
            .eq("cargo", "admin")
            .eq("status", "ativo")
            .eq("is_active", True)
            .limit(1)
            .execute()
        )
        rows = getattr(resp, "data", None) or []
        return bool(rows)
    except Exception as e:
        print(f"[AUTH] Aviso ao verificar admin do clube {club_id}: {type(e).__name__}: {e}")
        return False


def _extract_token(authorization: Optional[str] = None, access_token: Optional[str] = None) -> str:
    if authorization and authorization.lower().startswith("bearer "):
        return authorization.split(" ", 1)[1].strip()
    if access_token:
        return str(access_token).strip()
    raise HTTPException(401, "Token de acesso ausente")


def get_current_user(authorization: Optional[str] = Header(None), access_token: Optional[str] = Query(None)):
    token = _extract_token(authorization, access_token)
    payload = decode_access_token(token)
    user_id = payload.get("sub")
    if not user_id:
        raise HTTPException(401, "Token sem usuario")
    row = _get_app_user_by_id(str(user_id))
    if not row:
        raise HTTPException(401, "Usuario nao encontrado")
    if not bool(row.get("is_active", True)):
        raise HTTPException(403, "Conta inativa")
    status_value = row.get("status") or "ativo"
    if status_value == "bloqueado":
        raise HTTPException(403, "Conta bloqueada")
    if status_value == "pendente":
        raise HTTPException(403, "Conta pendente de liberacao")
    return _public_user(row)


def require_admin(current_user: dict = Depends(get_current_user)):
    if (current_user or {}).get("cargo") != "admin":
        raise HTTPException(403, "Apenas administradores podem executar esta acao")
    return current_user



def resolve_club_for_auth(clube: str, platform: str = "auto") -> dict:
    """Resolve nome do clube para club_id usando EA e fallbacks locais/Supabase."""
    name = str(clube or "").strip()
    if not name:
        raise HTTPException(400, "Nome do clube obrigatorio")
    search = ea_client.search_club(name, platform or "auto")
    if not search.get("success"):
        search = fallback_search_from_local(name, platform or "auto") or {"success": False}
    if not search.get("success"):
        sb = get_supabase()
        if sb:
            try:
                resp = sb.table("clubs").select("club_id,name,platform").ilike("name", f"%{name}%").limit(1).execute()
                rows = getattr(resp, "data", None) or []
                if rows:
                    row = rows[0]
                    search = {
                        "success": True,
                        "clubId": str(row.get("club_id")),
                        "name": row.get("name") or name,
                        "platform": row.get("platform") or "common-gen5",
                    }
            except Exception as e:
                print(f"[AUTH] Aviso ao buscar clube no Supabase: {type(e).__name__}: {e}")
    if not search.get("success"):
        raise HTTPException(404, "Clube nao encontrado pelo nome informado")
    return {
        "club_id": str(search.get("clubId") or "").strip(),
        "clube": search.get("name") or name,
        "platform": search.get("platform") or "common-gen5",
    }
def _assert_same_club(current_user: dict, club_id: str):
    user_club = str((current_user or {}).get("club_id") or "")
    if user_club and str(club_id or "") and user_club != str(club_id):
        raise HTTPException(403, "Usuario nao pertence a este clube")
    return True
def summarize_matches_by_type(matches):
    resumo = {"liga": 0, "copa": 0, "amistoso": 0, "desconhecido": 0}
    for m in matches or []:
        mt = m.get("match_type") or "desconhecido"
        if mt not in resumo:
            resumo[mt] = 0
        resumo[mt] += 1
    return resumo


def parse_matches(matches_raw, our_club_id):
    """Processa lista de partidas"""
    result = []
    if not isinstance(matches_raw, list):
        return result
    
    for m in matches_raw:
        try:
            clubs = m.get("clubs", {})
            our = clubs.get(str(our_club_id), {})
            opp_id = next((k for k in clubs.keys() if k != str(our_club_id)), None)
            opp = clubs.get(opp_id, {}) if opp_id else {}
            
            our_goals = int(our.get("goals", 0))
            opp_goals = int(opp.get("goals", 0))
            
            if our_goals > opp_goals: result_str = "V"
            elif our_goals < opp_goals: result_str = "D"
            else: result_str = "E"
            
            # Detecta tipo de partida (liga / copa / amistoso)
            raw_match_type = str(m.get("matchType") or "").lower()
            origin = str(m.get("_origin") or "").lower()
            type_probe = f"{raw_match_type} {origin}"
            if "playoff" in type_probe:
                match_type_label = "copa"
            elif (
                "friendly" in type_probe
                or "friendlies" in type_probe
                or "amist" in type_probe
                or "gametype9" in type_probe
                or "gametype13" in type_probe
                or "gametype15" in type_probe
                or "gametype16" in type_probe
            ):
                match_type_label = "amistoso"
            elif "league" in type_probe or "liga" in type_probe:
                match_type_label = "liga"
            else:
                match_type_label = origin or "desconhecido"

            # Encontra MOM e enriquece estatisticas por jogador
            players = m.get("players", {}).get(str(our_club_id), {})
            mom = None
            mom_rating = 0.0
            players_with_ratings = []
            for pid, pdata in players.items():
                rating = float(pdata.get("rating", 0) or 0)
                player_name = pdata.get("playername", "Unknown")
                p_goals = int(float(pdata.get("goals", 0) or 0))
                p_assists = int(float(pdata.get("assists", 0) or 0))
                p_shots = int(float(pdata.get("shots", 0) or 0))
                p_passes_made = int(float(pdata.get("passesmade", pdata.get("passesMade", 0)) or 0))
                p_pass_att = int(float(pdata.get("passattempts", pdata.get("passAttempts", 0)) or 0))
                p_tackles = int(float(pdata.get("tacklesmade", pdata.get("tacklesMade", 0)) or 0))
                p_tackle_att = int(float(pdata.get("tackleattempts", pdata.get("tackleAttempts", 0)) or 0))
                p_saves = int(float(pdata.get("saves", 0) or 0))
                p_cleansheets = int(float(pdata.get("cleansheetsany", pdata.get("cleanSheets", 0)) or 0))
                p_red = int(float(pdata.get("redcards", 0) or 0))
                p_mom_flag = int(float(pdata.get("mom", 0) or 0))
                p_pos = pdata.get("pos", "?")

                pass_pct = round((p_passes_made / max(p_pass_att, 1)) * 100, 1) if p_pass_att else 0
                tackle_pct = round((p_tackles / max(p_tackle_att, 1)) * 100, 1) if p_tackle_att else 0

                # Nota sofisticada: combina rating + impacto + posicao
                # Base: rating EA. Bonus por gol, assist, MOM, defesa solida.
                sofi = rating
                if p_pos.lower() in ("goalkeeper", "gk"):
                    sofi += min(p_saves * 0.05, 1.5)  # ate +1.5 por defesas
                    if p_cleansheets:
                        sofi += 0.5
                    if opp_goals >= 4:
                        sofi -= 0.4
                elif p_pos.lower() in ("defender", "cb", "lb", "rb"):
                    sofi += p_tackles * 0.04
                    if p_cleansheets:
                        sofi += 0.6
                    if opp_goals >= 4:
                        sofi -= 0.3
                    sofi += p_assists * 0.3 + p_goals * 0.5
                elif p_pos.lower() in ("midfielder", "cm", "cdm", "cam", "lm", "rm"):
                    sofi += p_assists * 0.5 + p_goals * 0.5
                    if pass_pct >= 80:
                        sofi += 0.3
                    sofi += p_tackles * 0.02
                else:  # forward / st / lw / rw
                    sofi += p_goals * 0.7 + p_assists * 0.4
                    if p_shots >= 5 and p_goals == 0:
                        sofi -= 0.2
                # Modificadores globais
                if result_str == "V":
                    sofi += 0.15
                elif result_str == "D":
                    sofi -= 0.15
                if p_red:
                    sofi -= 1.0
                if p_mom_flag:
                    sofi += 0.3
                # Limita 0-10
                sofi = max(0.0, min(10.0, sofi))

                players_with_ratings.append({
                    "name": player_name,
                    "rating": round(rating, 2),
                    "sofi_rating": round(sofi, 2),
                    "pos": p_pos,
                    "goals": p_goals,
                    "assists": p_assists,
                    "shots": p_shots,
                    "passes_made": p_passes_made,
                    "pass_pct": pass_pct,
                    "tackles_made": p_tackles,
                    "tackle_pct": tackle_pct,
                    "saves": p_saves,
                    "clean_sheet": p_cleansheets,
                    "red": p_red,
                    "mom": p_mom_flag,
                })
                if rating > mom_rating:
                    mom_rating = rating
                    mom = player_name
            
            timestamp = int(m.get("timestamp", 0))
            date_str = datetime.fromtimestamp(timestamp).strftime("%d/%m/%Y") if timestamp else "—"
            opponent_name = opp.get("details", {}).get("name", "Adversário")
            raw_match_id = str(m.get("matchId") or m.get("matchid") or m.get("id") or "").strip()
            if not raw_match_id or raw_match_id.lower() in ("none", "null", "undefined", "0"):
                # Algumas respostas da EA nao trazem matchId. Sem esse ID estavel,
                # o SQLite substitui jogos diferentes e o historico parece diminuir.
                raw_match_id = (
                    f"{our_club_id}:{timestamp}:{opp_id or opponent_name}:"
                    f"{our_goals}-{opp_goals}:{m.get('_origin', '')}"
                )
            
            result.append({
                "match_id": raw_match_id,
                "opponent": opponent_name,
                "score": f"{our_goals}-{opp_goals}",
                "goals_for": our_goals,
                "goals_against": opp_goals,
                "result": result_str,
                "match_type": match_type_label,
                "match_type_raw": raw_match_type or origin,
                "date": date_str,
                "timestamp": timestamp,
                "mom": mom,
                "mom_rating": round(mom_rating, 1),
                "players_ratings": sorted(players_with_ratings, key=lambda x: x["sofi_rating"], reverse=True)
            })
        except Exception as e:
            print(f"[parse_matches] Erro: {e}")
    
    return sorted(result, key=lambda x: x.get("timestamp", 0), reverse=True)


def parse_players(members_data):
    """Processa dados dos jogadores"""
    if not members_data or "members" not in members_data:
        return []
    
    players = []
    for m in members_data.get("members", []):
        gp = int(m.get("gamesPlayed", 0))
        if gp == 0:
            continue
        
        goals = int(m.get("goals", 0))
        assists = int(m.get("assists", 0))
        passes_made = int(m.get("passesMade", 0))
        pass_attempts = int(m.get("passAttempts", 1))
        tackles_made = int(m.get("tacklesMade", 0))
        tackle_attempts = int(m.get("tackleAttempts", 1))
        rating = float(m.get("ratingAve", 0))
        mom = int(m.get("manOfTheMatch", 0))
        shots = int(m.get("shots", 0))
        pos = m.get("favoritePosition", "?")
        
        players.append({
            "name": m.get("name", "Unknown"),
            "position": pos,
            "games": gp,
            "rating": round(rating, 2),
            "goals": goals,
            "assists": assists,
            "shots": shots,
            "passes_made": passes_made,
            "pass_pct": round((passes_made / max(pass_attempts, 1)) * 100, 1),
            "tackles_made": tackles_made,
            "tackle_pct": round((tackles_made / max(tackle_attempts, 1)) * 100, 1),
            "mom": mom,
            "goals_per_game": round(goals / gp, 2),
            "assists_per_game": round(assists / gp, 2),
        })
    
    return sorted(players, key=lambda x: x["rating"], reverse=True)


def calc_opponent_avg(matches_list):
    """Calcula média de gols por adversário"""
    by_opp = {}
    for m in matches_list:
        opp = m["opponent"]
        if opp not in by_opp:
            by_opp[opp] = {"games": 0, "gf": 0, "ga": 0}
        by_opp[opp]["games"] += 1
        by_opp[opp]["gf"] += m["goals_for"]
        by_opp[opp]["ga"] += m["goals_against"]
    
    result = []
    for opp, d in by_opp.items():
        result.append({
            "opponent": opp,
            "games": d["games"],
            "avg_gf": round(d["gf"] / d["games"], 1),
            "avg_ga": round(d["ga"] / d["games"], 1)
        })
    return sorted(result, key=lambda x: x["avg_gf"], reverse=True)[:10]


def build_ideal_team(players_list, formation="3-5-2"):
    """Monta 11 ideal por formacao, funcao e melhor encaixe disponivel."""
    formation_slots = {
        "3-5-2": ["GK", "LCB", "CB", "RCB", "LM", "LCM", "CM", "RCM", "RM", "LST", "RST"],
        "4-3-3": ["GK", "LB", "LCB", "RCB", "RB", "LCM", "CM", "RCM", "LW", "ST", "RW"],
        "4-4-2": ["GK", "LB", "LCB", "RCB", "RB", "LM", "LCM", "RCM", "RM", "LST", "RST"],
        "4-2-3-1": ["GK", "LB", "LCB", "RCB", "RB", "LDM", "RDM", "LAM", "CAM", "RAM", "ST"],
        "4-1-2-1-2": ["GK", "LB", "LCB", "RCB", "RB", "CDM", "LCM", "RCM", "CAM", "LST", "RST"],
        "3-4-3": ["GK", "LCB", "CB", "RCB", "LM", "LCM", "RCM", "RM", "LW", "ST", "RW"],
        "5-3-2": ["GK", "LWB", "LCB", "CB", "RCB", "RWB", "LCM", "CM", "RCM", "LST", "RST"],
    }
    slot_descriptions = {
        "GK": "Goleiro - protege a meta e inicia a saida de bola.",
        "LB": "Lateral esquerdo - amplitude, cobertura e apoio pela esquerda.",
        "RB": "Lateral direito - amplitude, cobertura e apoio pela direita.",
        "LWB": "Ala esquerdo - corredor inteiro, apoio ofensivo e recomposicao.",
        "RWB": "Ala direito - corredor inteiro, apoio ofensivo e recomposicao.",
        "LCB": "Zagueiro pela esquerda - cobertura e primeira construcao.",
        "CB": "Zagueiro central - lidera a linha defensiva.",
        "RCB": "Zagueiro pela direita - cobertura e duelos laterais.",
        "CDM": "Volante - protege a defesa e organiza a saida.",
        "LDM": "Volante esquerdo - equilibrio, cobertura e passe curto.",
        "RDM": "Volante direito - equilibrio, cobertura e pressao.",
        "LCM": "Meia central esquerdo - conexao, apoio e chegada.",
        "CM": "Meia central - dita ritmo e liga defesa/ataque.",
        "RCM": "Meia central direito - conexao, apoio e chegada.",
        "LM": "Meia/ala esquerdo - amplitude e criacao pelo lado.",
        "RM": "Meia/ala direito - amplitude e criacao pelo lado.",
        "CAM": "Meia ofensivo - cria chances entre linhas.",
        "LAM": "Meia ofensivo esquerdo - corta para dentro e cria.",
        "RAM": "Meia ofensivo direito - corta para dentro e cria.",
        "LW": "Ponta esquerda - profundidade e finalizacao pelo lado.",
        "RW": "Ponta direita - profundidade e finalizacao pelo lado.",
        "ST": "Centroavante - referencia, gols e ataque a area.",
        "LST": "Atacante esquerdo - ataca espacos e combina por dentro.",
        "RST": "Atacante direito - ataca espacos e combina por dentro.",
    }
    slot_coords = {
        "GK": (50, 92), "LB": (18, 76), "LWB": (14, 68), "LCB": (35, 78), "CB": (50, 80), "RCB": (65, 78), "RB": (82, 76), "RWB": (86, 68),
        "CDM": (50, 64), "LDM": (40, 64), "RDM": (60, 64), "LCM": (36, 52), "CM": (50, 50), "RCM": (64, 52), "LM": (18, 48), "RM": (82, 48),
        "LAM": (34, 34), "CAM": (50, 34), "RAM": (66, 34), "LW": (24, 22), "RW": (76, 22), "ST": (50, 16), "LST": (42, 16), "RST": (58, 16),
    }
    family_by_position = {
        "goalkeeper": "GK", "gk": "GK",
        "defender": "DEF", "cb": "DEF", "lb": "DEF", "rb": "DEF", "lwb": "DEF", "rwb": "DEF",
        "midfielder": "MID", "cm": "MID", "cdm": "MID", "cam": "MID", "lm": "MID", "rm": "MID",
        "forward": "FWD", "st": "FWD", "cf": "FWD", "lw": "FWD", "rw": "FWD", "lf": "FWD", "rf": "FWD",
    }
    preferred = {
        "GK": ["GK"],
        "LB": ["DEF", "MID"], "RB": ["DEF", "MID"], "LWB": ["DEF", "MID"], "RWB": ["DEF", "MID"], "LCB": ["DEF"], "CB": ["DEF"], "RCB": ["DEF"],
        "CDM": ["MID", "DEF"], "LDM": ["MID", "DEF"], "RDM": ["MID", "DEF"], "LCM": ["MID"], "CM": ["MID"], "RCM": ["MID"], "LM": ["MID", "FWD"], "RM": ["MID", "FWD"],
        "CAM": ["MID", "FWD"], "LAM": ["MID", "FWD"], "RAM": ["MID", "FWD"], "LW": ["FWD", "MID"], "RW": ["FWD", "MID"], "ST": ["FWD"], "LST": ["FWD"], "RST": ["FWD"],
    }
    role_bonus = {
        "GK": lambda p: p.get("rating", 0) * 10,
        "DEF": lambda p: p.get("rating", 0) * 10 + p.get("tackle_pct", 0) * 0.08 + p.get("mom", 0) * 0.05,
        "MID": lambda p: p.get("rating", 0) * 10 + p.get("assists_per_game", 0) * 4 + p.get("pass_pct", 0) * 0.05,
        "FWD": lambda p: p.get("rating", 0) * 10 + p.get("goals_per_game", 0) * 5 + p.get("assists_per_game", 0) * 2,
    }

    slots = formation_slots.get(formation, formation_slots["3-5-2"])
    pool = []
    for p in players_list or []:
        fam = family_by_position.get(str(p.get("position", "")).lower(), "MID")
        pool.append({**p, "family": fam})
    pool.sort(key=lambda p: float(p.get("rating", 0) or 0), reverse=True)

    team = []
    used = set()
    for slot in slots:
        wanted = preferred.get(slot, ["MID"])
        best = None
        best_score = -999
        for p in pool:
            if p.get("name") in used:
                continue
            fam = p.get("family", "MID")
            fit_bonus = 18 if fam == wanted[0] else 9 if fam in wanted else -12
            score = role_bonus.get(wanted[0], role_bonus["MID"])(p) + fit_bonus
            if score > best_score:
                best_score = score
                best = p
        if best:
            used.add(best.get("name"))
            x, y = slot_coords.get(slot, (50, 50))
            fit = "natural" if best.get("family") == wanted[0] else "adaptado" if best.get("family") in wanted else "improvisado"
            team.append({
                **best,
                "field_pos": slot,
                "role": slot,
                "role_description": slot_descriptions.get(slot, slot),
                "fit": fit,
                "x": x,
                "y": y,
                "selection_score": round(best_score, 1),
            })

    return {
        "formation": formation,
        "formation_name": f"Formacao {formation}",
        "slots": slots,
        "players": team,
        "missing_slots": [s for s in slots if not any(p.get("role") == s for p in team)],
        "available_formations": list(formation_slots.keys()),
    }

# ============================================================
# FASTAPI APP
# ============================================================

@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    yield

app = FastAPI(title=APP_NAME, lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

ea_client = EAFCClient()


# ============================================================
# ROTAS API
# ============================================================

@app.get("/health")
def health():
    return {"status": "ok", "app": APP_NAME}



@app.post("/api/auth/register")
def auth_register(payload: AuthRegisterPayload):
    sb = get_supabase()
    if not sb:
        raise HTTPException(503, "Supabase nao configurado para autenticacao")

    nome = str(payload.nome or "").strip()
    usuario = str(payload.usuario or "").strip().lower()
    senha = str(payload.senha or "")
    clube_informado = str(payload.clube or "").strip()

    if not nome or not usuario or not senha or not clube_informado:
        raise HTTPException(400, "Nome, usuario, senha e clube sao obrigatorios")
    if len(senha) < 6:
        raise HTTPException(400, "A senha deve ter pelo menos 6 caracteres")
    if _get_app_user_by_usuario(usuario):
        raise HTTPException(409, "Usuario ja existe")

    resolved = resolve_club_for_auth(clube_informado)
    club_id = resolved["club_id"]
    clube = resolved["clube"]

    row = {
        "club_id": club_id,
        "clube": clube,
        "nome": nome,
        "usuario": usuario,
        "password_hash": hash_password(senha),
        "cargo": "jogador",
        "status": "ativo",
        "is_active": True,
        "updated_at": _now_iso(),
    }
    try:
        resp = sb.table("app_users").insert(row).execute()
        rows = getattr(resp, "data", None) or []
        user = _public_user(rows[0] if rows else {**row, "id": ""})
        return {"success": True, "user": user}
    except Exception as e:
        print(f"[AUTH] Erro criando usuario: {type(e).__name__}: {e}")
        raise HTTPException(500, "Erro ao criar usuario")


@app.post("/api/auth/login")
def auth_login(payload: AuthLoginPayload):
    usuario = str(payload.usuario or "").strip().lower()
    senha = str(payload.senha or "")
    if not usuario or not senha:
        raise HTTPException(400, "Usuario e senha sao obrigatorios")

    row = _get_app_user_by_usuario(usuario)
    if not row or not verify_password(senha, row.get("password_hash")):
        raise HTTPException(401, "Usuario ou senha invalidos")
    if not bool(row.get("is_active", True)):
        raise HTTPException(403, "Conta inativa")
    status_value = row.get("status") or "ativo"
    if status_value == "bloqueado":
        raise HTTPException(403, "Conta bloqueada")
    if status_value == "pendente":
        raise HTTPException(403, "Conta pendente de liberacao")

    user = _public_user(row)
    if user.get("cargo") != "admin" and not club_has_admin(user.get("club_id")):
        raise HTTPException(403, "Este clube ainda nao possui um administrador cadastrado.")
    token = create_access_token({
        "sub": user["id"],
        "usuario": user["usuario"],
        "nome": user["nome"],
        "club_id": user["club_id"],
        "clube": user["clube"],
        "cargo": user["cargo"],
    })
    return {"success": True, "access_token": token, "token_type": "bearer", "user": user}


@app.get("/api/auth/me")
def auth_me(current_user: dict = Depends(get_current_user)):
    return {"success": True, "user": current_user}


@app.post("/api/auth/logout")
def auth_logout():
    return {"success": True}

@app.get("/api/test-search")
def test_search(
    club_name: str = Query("DESAGREGADOS SC"),
    platform: str = Query("common-gen5")
):
    """Endpoint de diagnostico: testa busca direta na EA FC API"""
    import requests as rq
    results = {}
    
    # Tenta com curl_cffi (impersonating chrome)
    try:
        r = ea_client.search_club(club_name, platform)
        results["curl_cffi"] = r
    except Exception as e:
        results["curl_cffi"] = {"error": str(e)}
    
    # Tenta com requests simples
    try:
        url = "https://proclubs.ea.com/api/fc/allTimeLeaderboard/search"
        headers = {
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:109.0) Gecko/20100101 Firefox/112.0",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Encoding": "gzip, deflate, br",
            "Referer": "https://www.ea.com/",
            "Origin": "https://www.ea.com",
        }
        params = {"clubName": club_name, "platform": platform}
        resp = rq.get(url, headers=headers, params=params, timeout=20)
        results["requests_simples"] = {
            "status": resp.status_code,
            "body_preview": resp.text[:500],
            "json": resp.json() if resp.status_code == 200 else None,
        }
    except Exception as e:
        results["requests_simples"] = {"error": f"{type(e).__name__}: {e}"}
    
    return results



class HistoryImportPayload(BaseModel):
    club: Dict[str, Any]
    matches: List[Dict[str, Any]] = []
    players: Optional[List[Dict[str, Any]]] = None


@app.post("/api/import-history")
def import_history(payload: HistoryImportPayload, current_user: dict = Depends(require_admin)):
    """Reidrata o cache/SQLite com o histórico salvo no navegador antes da sync."""
    club = payload.club or {}
    club_id = str(club.get("id") or club.get("clubId") or "").strip()
    if not club_id:
        raise HTTPException(400, "club.id obrigatorio")

    incoming_matches = [m for m in (payload.matches or []) if isinstance(m, dict)]
    incoming_players = [p for p in (payload.players or []) if isinstance(p, dict)]
    cache = load_cache() or {}
    club_json = load_club_json_history(club_id) or {}
    cache_club = cache.get("club") or {}
    json_club = club_json.get("club") or {}
    cache_matches = cache.get("matches") or [] if str(cache_club.get("id") or "") == club_id else []
    json_matches = club_json.get("matches") or [] if str(json_club.get("id") or club_id) == club_id else []
    cache_players = cache.get("players") or [] if str(cache_club.get("id") or "") == club_id else []
    json_players = club_json.get("players") or [] if str(json_club.get("id") or club_id) == club_id else []

    merged_matches = merge_match_lists_for_storage(club_id, json_matches, cache_matches, incoming_matches)
    players = incoming_players or cache_players or json_players

    imported_to_db = 0
    try:
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        for m in merged_matches:
            mid = stable_match_id_for_storage(m, club_id)
            c.execute(
                "INSERT OR REPLACE INTO matches (match_id, club_id, opponent, score, result, match_type, timestamp, data) VALUES (?,?,?,?,?,?,?,?)",
                (
                    mid,
                    club_id,
                    m.get("opponent", ""),
                    m.get("score", ""),
                    m.get("result", ""),
                    m.get("match_type", ""),
                    int(m.get("timestamp", 0) or 0),
                    json.dumps({**m, "match_id": mid}, ensure_ascii=False, default=str),
                ),
            )
            imported_to_db += 1
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"[IMPORT] Aviso ao importar historico local para SQLite: {e}")

    if not cache or str(cache_club.get("id") or "") == club_id:
        cache = {
            **cache,
            "club": {
                "id": club_id,
                "name": club.get("name") or cache_club.get("name") or "Clube",
                "platform": club.get("platform") or cache_club.get("platform") or "common-gen5",
                "synced_at": cache_club.get("synced_at") or datetime.now().isoformat(),
            },
            "players": players,
            "matches": merged_matches,
            "matchtype_summary": summarize_matches_by_type(merged_matches),
        }
        try:
            cache["stats"] = calc_club_stats([], {}, merged_matches)
            cache["opponents"] = calc_opponent_avg(merged_matches)
            if players:
                cache["ideal_team"] = build_ideal_team(players, "3-5-2")
                cache["mvp"] = max(players, key=lambda p: (p.get("mom", 0), p.get("rating", 0)))
            save_cache(cache)
            save_club_json_history(club_id, cache)
        except Exception as e:
            print(f"[IMPORT] Aviso ao atualizar cache importado: {e}")

    return {
        "success": True,
        "club_id": club_id,
        "received": len(incoming_matches),
        "total_matches": len(merged_matches),
        "db_imported": imported_to_db,
    }

@app.get("/api/dashboard")
def get_dashboard(current_user: dict = Depends(get_current_user)):
    """Retorna dados do cache JSON"""
    cache = load_cache()
    if not cache:
        cache = load_latest_club_data_supabase()
        if cache:
            try:
                save_cache(cache)
                save_club_json_history(str((cache.get("club") or {}).get("id") or "default"), cache)
            except Exception as e:
                print(f"[SUPABASE] Dashboard carregou, mas fallback local falhou: {e}")
    if cache:
        # Reconstroi time ideal se nao existir ou estiver vazio
        if cache.get("players") and (
            not cache.get("ideal_team")
            or not cache["ideal_team"].get("players")
        ):
            cache["ideal_team"] = build_ideal_team(cache["players"], "3-5-2")
            save_cache(cache)
        try:
            cache["player_profiles"] = load_player_profiles(str((cache.get("club") or {}).get("id") or "default"))
        except Exception as e:
            print(f"[profiles] dashboard sem perfis: {e}")
            cache.setdefault("player_profiles", {})
        return cache
    return {"club": None, "stats": None, "players": [], "matches": [], "opponents": [], "ideal_team": None}


@app.get("/api/ideal-team")
def get_ideal_team(formation: str = Query("3-5-2"), current_user: dict = Depends(get_current_user)):
    """Retorna o melhor 11 recalculado para a formacao escolhida."""
    cache = load_cache()
    if not cache or not cache.get("players"):
        raise HTTPException(404, "Sincronize um clube primeiro")
    return build_ideal_team(cache.get("players", []), formation)


@app.get("/api/test-matchtypes")
def test_matchtypes(
    club_name: str = Query("DESAGREGADOS SC"),
    platform: str = Query("auto"),
    max_count: int = Query(100, ge=1, le=100)
):
    """Diagnostico: busca o clube e testa todos os matchTypes conhecidos/candidatos."""
    search = ea_client.search_club(club_name, platform)
    if not search.get("success"):
        search = fallback_search_from_local(club_name, platform) or {"success": False}
    if not search.get("success"):
        raise HTTPException(404, "Clube nao encontrado")

    club_id = str(search["clubId"])
    plat = search.get("platform", platform) or "common-gen5"
    all_matches_raw, debug = fetch_all_match_types(ea_client, club_id, plat, max_count=max_count)
    parsed = parse_matches(all_matches_raw, club_id)
    resumo = summarize_matches_by_type(parsed)

    return {
        "club": {
            "id": club_id,
            "name": search.get("name"),
            "platform": plat,
        },
        "debug_matchtypes": debug,
        "total_raw": len(all_matches_raw),
        "total_parsed": len(parsed),
        "resumo_por_tipo": resumo,
        "matches_preview": parsed[:10],
    }


@app.get("/api/sync-stream")
async def sync_stream(
    club_name: str = Query("DESAGREGADOS SC"),
    platform: str = Query("auto"),
    access_token: str = Query("")
):
    """Sincronização com progresso em tempo real (SSE)"""
    current_user = get_current_user(authorization=None, access_token=access_token)
    if current_user.get("cargo") != "admin":
        raise HTTPException(403, "Apenas administradores podem sincronizar o clube")
    initial_platform = platform
    
    async def event_generator():
        plat = initial_platform
        start = time.time()
        
        def log(msg, step, total):
            elapsed = round(time.time() - start, 1)
            return json.dumps({
                "msg": f"[{elapsed}s] {msg}",
                "step": step,
                "total": total
            })
        
        try:
            yield f"data: {log('🔌 Conectando à EA FC...', 1, 8)}\n\n"
            await asyncio.sleep(0.1)
            
            yield f"data: {log(f'🔎 Buscando clube: {club_name} (varias plataformas)...', 2, 8)}\n\n"
            await asyncio.sleep(0.05)
            search = ea_client.search_club(club_name, plat)
            
            if not search.get("success"):
                fallback = fallback_search_from_local(club_name, plat)
                if fallback:
                    search = fallback
                    fallback_source = fallback.get("source", "local")
                    yield f"data: {log(f'Busca por nome falhou; usando clube salvo em {fallback_source}', 2, 8)}\n\n"
                else:
                    yield f"data: {log('❌ Clube nao encontrado em nenhuma plataforma nem no cache local', 2, 8)}\n\n"
                    yield f"data: {log('💡 Verifique o nome exato do clube e tente novamente', 2, 8)}\n\n"
                    yield f"data: {json.dumps({'error': 'Clube nao encontrado', 'done': True})}\n\n"
                    return
            
            club_id = str(search["clubId"])
            _assert_same_club(current_user, club_id)
            club_name_real = search["name"]
            plat = search.get("platform", plat) or "common-gen5"
            yield f"data: {log(f'✓ Clube: {club_name_real} (ID: {club_id}, plat: {plat})', 3, 8)}\n\n"
            
            yield f"data: {log('📊 Carregando estatísticas gerais...', 4, 8)}\n\n"
            overall = ea_client.overall_stats(club_id, plat)
            info = ea_client.club_info(club_id, plat)
            
            yield f"data: {log('👥 Baixando jogadores...', 5, 8)}\n\n"
            members = ea_client.members(club_id, plat)
            players = parse_players(members)
            yield f"data: {log(f'✓ {len(players)} jogadores carregados', 5, 8)}\n\n"
            
            yield f"data: {log('Baixando partidas e testando matchTypes da EA...', 6, 8)}\n\n"
            all_matches_raw, debug_matchtypes = fetch_all_match_types(ea_client, club_id, plat, max_count=100)
            for d in debug_matchtypes:
                status = "ok" if d.get("ok") else "falhou"
                err = f" | erro: {d.get('error')}" if d.get("error") else ""
                attempts = ", ".join(
                    f"{a.get('requested')}=>{a.get('count', 0)} (+{a.get('unique_added', 0)})"
                    for a in d.get("attempts", [])
                ) or str(d.get("requested", 100))
                mt_msg = (
                    f"matchType={d.get('matchType')} | tentativas {attempts} "
                    f"| novos={d.get('unique_added', 0)} | duplicados={d.get('duplicates', 0)} ({status}){err}"
                )
                yield f"data: {log(mt_msg, 7, 8)}\n\n"
                await asyncio.sleep(0.01)

            new_matches = parse_matches(all_matches_raw, club_id)
            resumo_sync = summarize_matches_by_type(new_matches)
            resumo_msg = (
                f"Resumo: liga={resumo_sync.get('liga', 0)}, "
                f"copa={resumo_sync.get('copa', 0)}, "
                f"amistoso={resumo_sync.get('amistoso', 0)}, "
                f"desconhecido={resumo_sync.get('desconhecido', 0)}"
            )
            yield f"data: {log(resumo_msg, 7, 8)}\n\n"
            print(f"[EA FC] Amistosos encontrados nesta sync: {resumo_sync.get('amistoso', 0)}")
            yield f"data: {log(f'✓ {len(new_matches)} partidas baixadas nesta sync', 7, 8)}\n\n"
            
            # ACUMULACAO HISTORICA: salva no DB e tambem preserva partidas antigas do cache.
            previous_cache = load_cache() or {}
            club_json_cache = load_club_json_history(club_id) or {}
            previous_matches = []
            previous_players = []
            previous_club = previous_cache.get("club") or {}
            club_json_club = club_json_cache.get("club") or {}
            if str(previous_club.get("id") or "") == club_id:
                previous_matches = previous_cache.get("matches") or []
                previous_players = previous_cache.get("players") or []
            if str(club_json_club.get("id") or club_id) == club_id:
                previous_matches = merge_match_lists_for_storage(club_id, club_json_cache.get("matches") or [], previous_matches)
                if not previous_players:
                    previous_players = club_json_cache.get("players") or []

            if not players and previous_players:
                players = previous_players
                yield f"data: {log(f'EA nao retornou jogadores agora; mantendo {len(previous_players)} jogadores do cache', 8, 8)}\n\n"

            if not new_matches and previous_matches:
                yield f"data: {log('EA nao retornou partidas novas; mantendo historico antigo sem apagar nada', 8, 8)}\n\n"
            def merge_match_lists(*groups):
                merged = {}
                fallback_i = 0
                for group in groups:
                    for item in group or []:
                        if not isinstance(item, dict):
                            continue
                        mid = str(item.get("match_id") or item.get("matchId") or "").strip()
                        if not mid or mid.lower() in ("none", "null", "undefined", "0"):
                            mid = f"fallback:{club_id}:{item.get('timestamp', '')}:{item.get('opponent', '')}:{item.get('score', '')}:{item.get('match_type', '')}:{fallback_i}"
                            fallback_i += 1
                        current = merged.get(mid, {})
                        merged[mid] = {**current, **item}
                return sorted(merged.values(), key=lambda x: int(x.get("timestamp", 0) or 0), reverse=True)

            try:
                conn = sqlite3.connect(DB_FILE)
                c = conn.cursor()
                for m in new_matches:
                    c.execute(
                        "INSERT OR REPLACE INTO matches (match_id, club_id, opponent, score, result, match_type, timestamp, data) VALUES (?,?,?,?,?,?,?,?)",
                        (
                            str(m.get("match_id") or f"{club_id}:{m.get('timestamp', '')}:{m.get('opponent', '')}:{m.get('score', '')}:{m.get('match_type', '')}"),
                            club_id,
                            m.get("opponent", ""),
                            m.get("score", ""),
                            m.get("result", ""),
                            m.get("match_type", ""),
                            int(m.get("timestamp", 0) or 0),
                            json.dumps(m, default=str),
                        ),
                    )
                conn.commit()
                c.execute(
                    "SELECT data FROM matches WHERE club_id=? AND data IS NOT NULL ORDER BY timestamp DESC",
                    (club_id,),
                )
                rows = c.fetchall()
                conn.close()
                db_matches = []
                for (raw,) in rows:
                    try:
                        db_matches.append(json.loads(raw))
                    except Exception:
                        pass
                matches = merge_match_lists(previous_matches, db_matches, new_matches)
                if previous_matches and len(matches) > len(new_matches):
                    yield f"data: {log(f'Cache preservado: {len(previous_matches)} partidas antigas foram consideradas', 8, 8)}\n\n"
                yield f"data: {log(f'Historico acumulado: {len(matches)} partidas totais', 8, 8)}\n\n"
            except Exception as e:
                print(f"[DB] Aviso ao acumular partidas: {e}")
                matches = merge_match_lists(previous_matches, new_matches)
                yield f"data: {log(f'Historico via cache: {len(matches)} partidas totais', 8, 8)}\n\n"
            yield f"data: {log('🧮 Calculando estatísticas...', 8, 8)}\n\n"
            stats = calc_club_stats(overall, info, matches)
            opponents = calc_opponent_avg(matches)
            ideal_team = build_ideal_team(players, "3-5-2")
            
            # Encontra MVP (jogador com mais MOMs)
            mvp = None
            if players:
                mvp = max(players, key=lambda p: (p["mom"], p["rating"]))
            
            # Monta dados completos
            club_data = {
                "club": {
                    "id": club_id,
                    "name": club_name_real,
                    "platform": plat,
                    "synced_at": datetime.now().isoformat()
                },
                "stats": stats,
                "players": players,
                "matches": matches,
                "opponents": opponents,
                "ideal_team": ideal_team,
                "mvp": mvp,
                "debug_matchtypes": debug_matchtypes,
                "matchtype_summary": summarize_matches_by_type(matches),
            }
            
            try:
                if _env_flag("USE_SUPABASE"):
                    yield f"data: {log('Salvando e carregando histórico no Supabase...', 8, 8)}\n\n"
                    save_club_supabase(club_data)
                    save_players_supabase(club_id, players)
                    save_matches_supabase(club_id, matches)

                    supabase_matches = load_matches_supabase(club_id, limit=1000)
                    if supabase_matches:
                        matches = merge_match_lists_for_storage(club_id, supabase_matches, matches)
                        stats = calc_club_stats(overall, info, matches)
                        opponents = calc_opponent_avg(matches)
                        ideal_team = build_ideal_team(players, "3-5-2")
                        if players:
                            mvp = max(players, key=lambda p: (p.get("mom", 0), p.get("rating", 0)))
                        club_data["matches"] = matches
                        club_data["stats"] = stats
                        club_data["opponents"] = opponents
                        club_data["ideal_team"] = ideal_team
                        club_data["mvp"] = mvp
                        club_data["matchtype_summary"] = summarize_matches_by_type(matches)
                        yield f"data: {log(f'Histórico Supabase carregado: {len(matches)} partidas totais', 8, 8)}\n\n"
                        save_club_supabase(club_data)

                    log_sync_supabase(
                        club_id=club_id,
                        platform=plat,
                        status="success",
                        total_matches=len(matches),
                        new_matches=len(new_matches),
                        message="Sincronização concluída com Supabase",
                        debug=debug_matchtypes,
                    )
            except Exception as supabase_err:
                print(f"[SUPABASE] Aviso: {type(supabase_err).__name__}: {supabase_err}")
                try:
                    log_sync_supabase(
                        club_id=club_id,
                        platform=plat,
                        status="partial",
                        total_matches=len(matches),
                        new_matches=len(new_matches),
                        message=f"Supabase falhou, seguindo com cache local: {supabase_err}",
                        debug=debug_matchtypes,
                    )
                except Exception:
                    pass
                yield f"data: {log('Supabase falhou; seguindo com cache local/JSON/SQLite', 8, 8)}\n\n"
            # Salva cache JSON principal e histórico por clube
            save_cache(club_data)
            save_club_json_history(club_id, club_data)
            
            # Salva no DB (best-effort, nao falha a sync se DB der erro)
            try:
                conn = sqlite3.connect(DB_FILE)
                c = conn.cursor()
                c.execute(
                    "INSERT OR REPLACE INTO clubs (club_id, name, platform, data) VALUES (?, ?, ?, ?)",
                    (club_id, club_name_real, plat, json.dumps(club_data, default=str))
                )
                conn.commit()
                conn.close()
            except Exception as db_err:
                # DB legado pode nao ter a coluna data; cache JSON eh fonte primaria
                print(f"[DB] Aviso: nao salvou no SQLite: {db_err}")
                yield f"data: {log(f'⚠️ DB legado ignorado: {db_err}', 8, 8)}\n\n"
            
            yield f"data: {log(f'✅ Sincronização completa!', 8, 8)}\n\n"
            yield f"data: {log(f'💾 Dados salvos em {JSON_CACHE}', 8, 8)}\n\n"
            yield f"data: {json.dumps({'done': True, 'success': True, 'club': club_name_real})}\n\n"
            
        except Exception as e:
            yield f"data: {log(f'❌ Erro: {str(e)}', 0, 8)}\n\n"
            yield f"data: {json.dumps({'error': str(e), 'done': True})}\n\n"
    
    return StreamingResponse(event_generator(), media_type="text/event-stream")


def _position_profile(position: str) -> str:
    pos = (position or "").lower()
    if pos in ("goalkeeper", "gk"):
        return "GK"
    if pos in ("defender", "cb", "lb", "rb", "lcb", "rcb", "rwb", "lwb"):
        return "DEF"
    if pos in ("midfielder", "cm", "cdm", "cam", "lm", "rm"):
        return "MID"
    if pos in ("forward", "st", "cf", "lw", "rw", "lf", "rf"):
        return "FWD"
    return "MID"


def _avg(values, default=0):
    vals = [float(v) for v in values if v is not None]
    return round(sum(vals) / len(vals), 2) if vals else default


def _safe_pct(value, total):
    return round((float(value) / max(float(total), 1)) * 100, 1) if total else 0


def build_estimated_heatmap(player, history):
    """Mapa de calor estimado por perfil estatistico, sem coordenadas reais da EA."""
    profile = _position_profile(player.get("position"))
    zones = {
        "def_left": 0, "def_center": 0, "def_right": 0,
        "mid_left": 0, "mid_center": 0, "mid_right": 0,
        "att_left": 0, "att_center": 0, "att_right": 0,
    }
    if profile == "GK":
        zones.update({"def_center": 70, "def_left": 20, "def_right": 20, "mid_center": 8})
    elif profile == "DEF":
        zones.update({"def_center": 45, "def_left": 28, "def_right": 28, "mid_center": 22})
    elif profile == "MID":
        zones.update({"mid_center": 55, "mid_left": 24, "mid_right": 24, "def_center": 16, "att_center": 18})
    else:
        zones.update({"att_center": 55, "att_left": 24, "att_right": 24, "mid_center": 18})

    for h in history:
        goals = h.get("goals", 0)
        assists = h.get("assists", 0)
        shots = h.get("shots", 0)
        tackles = h.get("tackles_made", 0)
        saves = h.get("saves", 0)
        clean = h.get("clean_sheet", 0)
        pos = (h.get("position") or player.get("position") or "").lower()
        zones["att_center"] += goals * 8 + shots * 2
        zones["att_left"] += assists * 3
        zones["att_right"] += assists * 3
        zones["mid_center"] += assists * 5 + h.get("pass_pct", 0) * 0.05
        zones["def_center"] += tackles * 3 + saves * 6 + clean * 5
        if "lb" in pos or "lm" in pos or "lw" in pos:
            zones["def_left"] += tackles * 2
            zones["mid_left"] += assists * 2 + shots
            zones["att_left"] += goals * 3 + shots
        elif "rb" in pos or "rm" in pos or "rw" in pos:
            zones["def_right"] += tackles * 2
            zones["mid_right"] += assists * 2 + shots
            zones["att_right"] += goals * 3 + shots
        else:
            zones["mid_center"] += tackles + assists

    max_val = max(zones.values()) if zones else 1
    return {
        "disclaimer": "Mapa de calor estimado por perfil estatistico; a API nao fornece coordenadas reais de campo.",
        "profile": profile,
        "zones": {k: round(min(1, v / max(max_val, 1)), 3) for k, v in zones.items()},
    }


def build_player_analytics(player_name, cache, match_type="todos"):
    """Gera pacote analitico profissional do jogador a partir do cache local."""
    if not cache:
        raise HTTPException(404, "Sincronize um clube primeiro")
    pname = player_name.strip().lower()
    players = cache.get("players", [])
    player = next((p for p in players if p.get("name", "").lower() == pname), None)

    wanted_match_type = (match_type or "todos").lower()
    history = []
    for m in cache.get("matches", []):
        current_match_type = str(m.get("match_type", "desconhecido") or "desconhecido").lower()
        if wanted_match_type != "todos" and current_match_type != wanted_match_type:
            continue
        for pr in (m.get("players_ratings") or []):
            if pr.get("name", "").lower() == pname:
                history.append({
                    "match_id": m.get("match_id"), "opponent": m.get("opponent"), "date": m.get("date"),
                    "timestamp": int(m.get("timestamp", 0) or 0), "score": m.get("score"), "result": m.get("result"),
                    "match_type": m.get("match_type", "liga"), "position": pr.get("pos"),
                    "rating": float(pr.get("rating", 0) or 0),
                    "sofi_rating": float(pr.get("sofi_rating", pr.get("rating", 0)) or 0),
                    "goals": int(pr.get("goals", 0) or 0), "assists": int(pr.get("assists", 0) or 0),
                    "shots": int(pr.get("shots", 0) or 0), "passes_made": int(pr.get("passes_made", 0) or 0),
                    "pass_pct": float(pr.get("pass_pct", 0) or 0), "tackles_made": int(pr.get("tackles_made", 0) or 0),
                    "tackle_pct": float(pr.get("tackle_pct", 0) or 0), "saves": int(pr.get("saves", 0) or 0),
                    "clean_sheet": int(pr.get("clean_sheet", 0) or 0), "red": int(pr.get("red", 0) or 0),
                    "mom": int(pr.get("mom", 0) or 0),
                })
                break

    history.sort(key=lambda x: x.get("timestamp", 0), reverse=True)
    if not history:
        print(f"[ANALYTICS] Jogador sem dados suficientes no clube/filtro atual: {player_name}")

    games = len(history)
    ratings = [h["rating"] for h in history]
    sofi = [h["sofi_rating"] for h in history]
    goals = sum(h["goals"] for h in history)
    assists = sum(h["assists"] for h in history)
    shots = sum(h["shots"] for h in history)
    tackles = sum(h["tackles_made"] for h in history)
    saves = sum(h["saves"] for h in history)
    clean_sheets = sum(h["clean_sheet"] for h in history)
    moms = sum(1 for h in history if h.get("mom"))
    reds = sum(h["red"] for h in history)

    # Todas as metricas de elenco/ranking abaixo vêm SOMENTE das partidas salvas do clube atual.
    # Nao usa games/goals globais de members/career/stats, porque esses numeros podem incluir outros clubes.
    club_player_rows = {}
    for m in cache.get("matches", []):
        current_match_type = str(m.get("match_type", "desconhecido") or "desconhecido").lower()
        if wanted_match_type != "todos" and current_match_type != wanted_match_type:
            continue
        for pr in (m.get("players_ratings") or []):
            nm = pr.get("name") or "Unknown"
            row = club_player_rows.setdefault(nm, {
                "name": nm, "position": pr.get("pos") or "?", "games": 0,
                "rating_sum": 0.0, "sofi_sum": 0.0, "goals": 0, "assists": 0, "shots": 0,
                "pass_pct_sum": 0.0, "tackle_pct_sum": 0.0, "mom": 0,
            })
            row["games"] += 1
            row["position"] = pr.get("pos") or row["position"]
            row["rating_sum"] += float(pr.get("rating", 0) or 0)
            row["sofi_sum"] += float(pr.get("sofi_rating", pr.get("rating", 0)) or 0)
            row["goals"] += int(pr.get("goals", 0) or 0)
            row["assists"] += int(pr.get("assists", 0) or 0)
            row["shots"] += int(pr.get("shots", 0) or 0)
            row["pass_pct_sum"] += float(pr.get("pass_pct", 0) or 0)
            row["tackle_pct_sum"] += float(pr.get("tackle_pct", 0) or 0)
            row["mom"] += int(pr.get("mom", 0) or 0)

    club_players = []
    for row in club_player_rows.values():
        gp = max(int(row.get("games", 0) or 0), 1)
        club_players.append({
            "name": row["name"],
            "position": row.get("position") or "?",
            "games": row["games"],
            "rating": round(row["rating_sum"] / gp, 2),
            "sofi_rating": round(row["sofi_sum"] / gp, 2),
            "goals": row["goals"],
            "assists": row["assists"],
            "shots": row["shots"],
            "pass_pct": round(row["pass_pct_sum"] / gp, 1),
            "tackle_pct": round(row["tackle_pct_sum"] / gp, 1),
            "mom": row["mom"],
            "goals_per_game": round(row["goals"] / gp, 2),
            "assists_per_game": round(row["assists"] / gp, 2),
        })

    player_club = next((p for p in club_players if p.get("name", "").lower() == pname), None)
    if player_club:
        base_player = player or {"name": player_club.get("name", player_name), "position": player_club.get("position", "?"), "rating": player_club.get("rating", 0)}
        player = {**base_player, **player_club, "ea_global_games": base_player.get("games"), "club_games": player_club.get("games", 0)}
    elif player:
        player = {**player, "games": 0, "club_games": 0, "ea_global_games": player.get("games")}
    else:
        raise HTTPException(404, f"Jogador '{player_name}' sem partidas no clube/filtro atual")

    team_rating_avg = _avg([p.get("rating", 0) for p in club_players], 0)
    team_goal_avg = _avg([p.get("goals_per_game", 0) for p in club_players], 0)
    ranking = sorted(club_players, key=lambda p: float(p.get("rating", 0) or 0), reverse=True)
    rank_position = next((i + 1 for i, p in enumerate(ranking) if p.get("name", "").lower() == pname), None)

    recent_asc = sorted(history[:8], key=lambda x: x.get("timestamp", 0))
    first_half = recent_asc[:max(1, len(recent_asc)//2)]
    second_half = recent_asc[max(1, len(recent_asc)//2):]
    delta = _avg([h["sofi_rating"] for h in second_half], 0) - _avg([h["sofi_rating"] for h in first_half], 0) if len(recent_asc) >= 4 else 0
    trend = "subindo" if delta >= 0.25 else "caindo" if delta <= -0.25 else "estavel"

    best_match = max(history, key=lambda h: h["sofi_rating"], default=None)
    worst_match = min(history, key=lambda h: h["sofi_rating"], default=None)
    by_opp = {}
    for h in history:
        by_opp.setdefault(h.get("opponent") or "Adversario", []).append(h["sofi_rating"])
    opponent_perf = [{"opponent": opp, "games": len(vals), "avg_sofi": round(sum(vals) / len(vals), 2)} for opp, vals in by_opp.items()]

    offensive_impact = goals * 8 + assists * 6 + shots * 1.2
    defensive_impact = tackles * 2 + _avg([h["tackle_pct"] for h in history], 0) * 0.2 + clean_sheets * 4 + saves * 3
    consistency_std = pstdev(sofi) if len(sofi) > 1 else 0
    consistency = max(0, 100 - consistency_std * 18)
    regularity = _safe_pct(sum(1 for r in ratings if r >= 7), games)
    clutch = moms * 8 + sum((h["goals"] + h["assists"]) * 3 for h in history if h.get("result") == "V")
    risk = reds * 12 + sum(1 for r in ratings if r < 6) * 4
    # Nota analitica calibrada para leitura humana: a base principal e a nota media EA/Sofi.
    # Impactos ofensivos/defensivos, regularidade e risco ajustam a nota, mas nao destroem um bom rating.
    base_rating_score = ((_avg(ratings, 0) + _avg(sofi, 0)) / 2) * 10
    impact_bonus = min(8, offensive_impact / max(games, 1) * 0.9) + min(8, defensive_impact / max(games, 1) * 0.65)
    consistency_bonus = (consistency - 70) * 0.08
    regularity_bonus = (regularity - 50) * 0.08
    clutch_bonus = min(5, clutch / max(games, 1) * 0.8)
    risk_penalty = min(10, risk / max(games, 1) * 1.2)
    sample_penalty = 4 if games and games < 5 else 0
    analytic_score = base_rating_score + impact_bonus + consistency_bonus + regularity_bonus + clutch_bonus - risk_penalty - sample_penalty
    analytic_score = round(max(0, min(100, analytic_score)), 1)

    radar = {
        "Finalizacao": round(min(100, (goals / max(games, 1)) * 45 + (shots / max(games, 1)) * 8), 1),
        "Criacao": round(min(100, (assists / max(games, 1)) * 55 + _avg([h["pass_pct"] for h in history], 0) * 0.25), 1),
        "Passe": round(min(100, _avg([h["pass_pct"] for h in history], player.get("pass_pct", 0))), 1),
        "Defesa": round(min(100, (tackles / max(games, 1)) * 18 + _avg([h["tackle_pct"] for h in history], 0) * 0.45 + saves * 2), 1),
        "Consistencia": round(consistency, 1),
        "Decisao": round(min(100, regularity * 0.45 + clutch * 2), 1),
    }

    analytics = {
        "player": player,
        "games_with_history": games,
        "scope": {"club_id": (cache.get("club") or {}).get("id"), "match_type": wanted_match_type, "label": "clube atual"},
        "history": history[:100],
        "series": list(reversed(history[:100])),
        "averages": {
            "rating": _avg(ratings, player.get("rating", 0)), "sofi_rating": _avg(sofi, player.get("rating", 0)),
            "goals_per_game": round(goals / max(games, 1), 2), "assists_per_game": round(assists / max(games, 1), 2),
            "shots_per_game": round(shots / max(games, 1), 2), "passes_pct": _avg([h["pass_pct"] for h in history], player.get("pass_pct", 0)),
            "tackle_pct": _avg([h["tackle_pct"] for h in history], player.get("tackle_pct", 0)), "tackles_per_game": round(tackles / max(games, 1), 2),
            "saves_per_game": round(saves / max(games, 1), 2),
        },
        "totals": {"goals": goals, "assists": assists, "shots": shots, "tackles": tackles, "moms": moms, "red_cards": reds, "clean_sheets": clean_sheets, "saves": saves},
        "advanced": {"offensive_impact": round(offensive_impact, 1), "defensive_impact": round(defensive_impact, 1), "consistency": round(consistency, 1), "regularity": regularity, "clutch_score": round(clutch, 1), "risk": round(risk, 1), "analytic_score": analytic_score, "radar": radar},
        "ranking": {"position": rank_position, "total_players": len(club_players), "rating_rank_label": f"{rank_position}/{len(club_players)}" if rank_position else "-"},
        "team_comparison": {"team_avg_rating": team_rating_avg, "team_scope": "clube_atual", "player_vs_team_rating": round(float(player.get("rating", 0) or 0) - team_rating_avg, 2), "team_avg_goals_per_game": team_goal_avg, "player_vs_team_goals_per_game": round(float(player.get("goals_per_game", 0) or 0) - team_goal_avg, 2)},
        "trend": {"status": trend, "delta_recent_sofi": round(delta, 2)},
        "best_match": best_match,
        "worst_match": worst_match,
        "best_opponent": max(opponent_perf, key=lambda x: x["avg_sofi"], default=None),
        "worst_opponent": min(opponent_perf, key=lambda x: x["avg_sofi"], default=None),
        "heatmap": build_estimated_heatmap(player, history),
        "scout_report": None,
    }
    analytics["scout_report"] = generate_player_scout_report_offline(analytics)
    return analytics


def generate_player_scout_report_offline(analytics):
    p = analytics["player"]
    avg = analytics["averages"]
    adv = analytics["advanced"]
    totals = analytics["totals"]
    trend = analytics["trend"]["status"]
    profile = _position_profile(p.get("position"))
    strengths = []
    weaknesses = []
    if avg["rating"] >= 7.5:
        strengths.append(f"Nota media alta ({avg['rating']}) e presenca confiavel.")
    if adv["regularity"] >= 70:
        strengths.append(f"Regularidade forte: {adv['regularity']}% dos jogos com rating >= 7.")
    if totals["goals"] or totals["assists"]:
        strengths.append(f"Impacto ofensivo direto: {totals['goals']} gols e {totals['assists']} assistencias.")
    if avg["passes_pct"] >= 75:
        strengths.append(f"Boa seguranca na circulacao: {avg['passes_pct']}% de passes.")
    if totals["tackles"] or totals["saves"] or totals["clean_sheets"]:
        strengths.append("Contribuicao defensiva relevante para o perfil da posicao.")
    if adv["risk"] > 10:
        weaknesses.append("Risco competitivo acima do ideal por cartoes ou notas baixas.")
    if adv["consistency"] < 65:
        weaknesses.append("Oscilacao de notas acima do desejado; precisa estabilizar desempenho.")
    if avg["passes_pct"] and avg["passes_pct"] < 65:
        weaknesses.append("Eficiencia de passe pode limitar a construcao.")
    if not strengths:
        strengths.append("Boa base estatistica, mas ainda sem destaque dominante no recorte disponivel.")
    if not weaknesses:
        weaknesses.append("Sem alerta grave; foco principal e manter constancia e tomada de decisao.")
    tactical = {
        "GK": "Goleiro de reacao, priorizando seguranca, reposicao curta e protecao de clean sheet.",
        "DEF": "Defensor de cobertura, com foco em antecipacao, linha compacta e saida simples.",
        "MID": "Meio-campista de conexao, ideal para dar ritmo, apoiar pressao e criar superioridade pelo passe.",
        "FWD": "Atacante de decisao, procurando volume de finalizacoes e participacao direta no ultimo terco.",
    }.get(profile, "Jogador de apoio tatico, util para equilibrar posse, pressao e transicao.")
    return f"""## Perfil do jogador
{p['name']} atua como {p.get('position', '-')}, com nota analitica final de **{adv['analytic_score']}/100** e media EA de **{avg['rating']}** nas partidas com historico.

## Pontos fortes
{chr(10).join(f'- {x}' for x in strengths)}

## Pontos fracos
{chr(10).join(f'- {x}' for x in weaknesses)}

## Tendencia recente
O momento recente esta **{trend}** (variacao sofi: {analytics['trend']['delta_recent_sofi']}).

## Funcao tatica ideal
{tactical}

## Comparacao com elenco
Ranking por nota: **{analytics['ranking']['rating_rank_label']}**. Diferenca para media do elenco: **{analytics['team_comparison']['player_vs_team_rating']}** pontos de rating.

## Recomendacao pratica
Priorize treinos e chamadas que maximizem os pontos fortes acima. Em jogos grandes, use o jogador em funcoes que reduzam risco e aumentem participacao nas zonas fortes do mapa estimado.
"""

@app.post("/api/ai/player")
async def ai_player(player_name: str, match_type: str = Query("todos")):
    """Analise de jogador por IA usando o pacote analitico completo."""
    cache = load_cache()
    if not cache or not cache.get("players"):
        raise HTTPException(404, "Sincronize um clube primeiro")

    analytics = build_player_analytics(player_name, cache, match_type)
    player = analytics["player"]

    if not OPENAI_API_KEY:
        analysis = generate_player_scout_report_offline(analytics)
    else:
        try:
            from openai import OpenAI
            client = OpenAI(api_key=OPENAI_API_KEY)
            prompt = f"""Analise o jogador de futebol abaixo do EA FC Pro Clubs e gere um relatorio scout profissional em portugues.

Dados analiticos JSON:
{json.dumps(analytics, ensure_ascii=False, indent=2, default=str)}

Responda em markdown com:
## Perfil do jogador
## Pontos fortes
## Pontos fracos
## Tendencia recente
## Funcao tatica ideal
## Comparacao com elenco
## Recomendacao pratica
"""
            resp = client.chat.completions.create(
                model=OPENAI_MODEL,
                messages=[{"role": "user", "content": prompt}],
                temperature=0.7,
            )
            analysis = resp.choices[0].message.content
        except Exception as e:
            print(f"[AI] Falha ao gerar analise OpenAI para {player_name}: {e}")
            analysis = generate_player_scout_report_offline(analytics)

    return {"player": player, "analytics": analytics, "analysis": analysis}

def generate_player_analysis_offline(p):
    """Análise offline baseada em estatísticas"""
    rating = p['rating']
    
    if rating >= 8: nivel = "EXCELENTE ⭐⭐⭐⭐⭐"
    elif rating >= 7.5: nivel = "MUITO BOM ⭐⭐⭐⭐"
    elif rating >= 7: nivel = "BOM ⭐⭐⭐"
    elif rating >= 6.5: nivel = "REGULAR ⭐⭐"
    else: nivel = "PRECISA MELHORAR ⭐"
    
    pontos_fortes = []
    pontos_fracos = []
    
    if p['pass_pct'] >= 75: pontos_fortes.append(f"Excelente precisão de passes ({p['pass_pct']}%)")
    elif p['pass_pct'] < 60: pontos_fracos.append(f"Precisão de passes baixa ({p['pass_pct']}%)")
    
    if p['tackle_pct'] >= 50: pontos_fortes.append(f"Bom em divididas ({p['tackle_pct']}%)")
    elif p['tackle_pct'] < 30: pontos_fracos.append(f"Divididas precisam melhorar ({p['tackle_pct']}%)")
    
    if p['goals_per_game'] >= 0.5: pontos_fortes.append(f"Artilheiro ({p['goals_per_game']} gols/jogo)")
    if p['mom'] >= 3: pontos_fortes.append(f"Decisivo: {p['mom']} MOMs")
    
    if not pontos_fortes: pontos_fortes.append("Atuação consistente")
    if not pontos_fracos: pontos_fracos.append("Continue evoluindo")
    
    return f"""## 📊 Análise: {p['name']}

**Posição:** {p['position']} | **Jogos:** {p['games']} | **Nível:** {nivel}

## ✅ Pontos Fortes
{chr(10).join(f'- {pf}' for pf in pontos_fortes)}

## ⚠️ Pontos a Melhorar
{chr(10).join(f'- {pf}' for pf in pontos_fracos)}

## 🎯 Recomendação Tática
{'Mantenha a regularidade. Jogador essencial para o time.' if rating >= 7.5 else 'Trabalhe consistência e participação ofensiva.'}

## 🏆 Nota Geral
**{rating}/10**
"""


@app.get("/api/ai/team")
async def ai_team(formation: str = Query("3-5-2")):
    """Análise do time ideal"""
    cache = load_cache()
    if not cache or not cache.get("players"):
        raise HTTPException(404, "Sincronize um clube primeiro")
    
    ideal = build_ideal_team(cache.get("players", []), formation)
    
    text = f"""## 🏆 Time Ideal — Formação {ideal['formation']}

### Escalação
"""
    for p in ideal["players"]:
        text += f"- **{p['field_pos']}** — {p['name']} (Nota: {p['rating']})\n"
    
    text += f"""
### 📋 Análise Tática

A formação **{ideal['formation']}** foi escolhida com base no elenco disponível, priorizando os jogadores com melhor desempenho em cada posição.

### 🎯 Pontos Fortes
- Equilíbrio entre defesa e ataque
- Aproveitamento dos jogadores em melhor fase
- Distribuição tática otimizada

### ⚡ Recomendações
- Manter intensidade no meio-campo
- Aproveitar laterais para ataques rápidos
- Pressão alta na recuperação de bola
"""
    return {"team": ideal, "analysis": text}


# ============================================================
# JOGADOR - DETALHES E HISTORICO
# ============================================================

@app.get("/api/player/{player_name}/analytics")
def get_player_analytics(player_name: str, match_type: str = Query("todos")):
    """Retorna analytics profissional completo do jogador."""
    cache = load_cache()
    return build_player_analytics(player_name, cache, match_type)


@app.get("/api/player/{player_name}")
def get_player_detail(player_name: str, current_user: dict = Depends(get_current_user)):
    """Retorna jogador + ultimas 100 partidas dele com nota sofisticada"""
    cache = load_cache()
    if not cache or not cache.get("players"):
        raise HTTPException(404, "Sincronize um clube primeiro")

    pname = player_name.strip().lower()
    player = next((p for p in cache["players"] if p["name"].lower() == pname), None)
    if not player:
        raise HTTPException(404, f"Jogador '{player_name}' nao encontrado")

    # Coleta histórico em todas as partidas
    history = []
    for m in cache.get("matches", []):
        for pr in (m.get("players_ratings") or []):
            if pr["name"].lower() == pname:
                history.append({
                    "match_id": m.get("match_id"),
                    "opponent": m.get("opponent"),
                    "date": m.get("date"),
                    "timestamp": m.get("timestamp"),
                    "score": m.get("score"),
                    "result": m.get("result"),
                    "match_type": m.get("match_type", "liga"),
                    "position": pr.get("pos"),
                    "rating": pr.get("rating"),
                    "sofi_rating": pr.get("sofi_rating", pr.get("rating")),
                    "goals": pr.get("goals", 0),
                    "assists": pr.get("assists", 0),
                    "shots": pr.get("shots", 0),
                    "pass_pct": pr.get("pass_pct", 0),
                    "tackle_pct": pr.get("tackle_pct", 0),
                    "saves": pr.get("saves", 0),
                    "mom": pr.get("mom", 0),
                })
                break

    # Ordena por data desc e pega 100
    history.sort(key=lambda x: x.get("timestamp", 0), reverse=True)
    history = history[:100]

    # Estatisticas agregadas do historico
    h_stats = {"games": len(history), "goals": 0, "assists": 0, "avg_rating": 0, "avg_sofi": 0, "moms": 0}
    if history:
        h_stats["goals"] = sum(h["goals"] for h in history)
        h_stats["assists"] = sum(h["assists"] for h in history)
        h_stats["avg_rating"] = round(sum(float(h["rating"]) for h in history) / len(history), 2)
        h_stats["avg_sofi"] = round(sum(float(h["sofi_rating"]) for h in history) / len(history), 2)
        h_stats["moms"] = sum(1 for h in history if h.get("mom"))

    return {"player": player, "history": history, "history_stats": h_stats}


# ============================================================
# CADASTRO DE JOGADORES - AJUSTES MANUAIS
# ============================================================

class PlayerProfileUpdate(BaseModel):
    manual_position: Optional[str] = None
    archetype: Optional[str] = None
    playstyles: Optional[List[str]] = None
    notes: Optional[str] = None


def _current_club_id_from_cache() -> str:
    cache = load_cache() or {}
    club = cache.get("club") or {}
    return str(club.get("id") or "default")


def load_player_profiles(club_id: Optional[str] = None) -> Dict[str, Dict[str, Any]]:
    club_id = str(club_id or _current_club_id_from_cache())
    profiles_sb = load_player_profiles_supabase(club_id)
    if profiles_sb:
        return profiles_sb
    try:
        conn = sqlite3.connect(DB_FILE)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT player_name, manual_position, archetype, playstyles, notes FROM player_profiles WHERE club_id=?",
            (club_id,),
        ).fetchall()
        conn.close()
        return {
            r["player_name"]: {
                "manual_position": r["manual_position"],
                "archetype": r["archetype"],
                "playstyles": json.loads(r["playstyles"] or "[]") if r["playstyles"] else [],
                "notes": r["notes"],
            }
            for r in rows
        }
    except Exception as e:
        print(f"[profiles] erro ao carregar perfis: {e}")
        return {}


@app.get("/api/player-profiles")
def list_player_profiles(current_user: dict = Depends(get_current_user)):
    """Lista ajustes manuais de posicao/arquetipo do clube sincronizado."""
    club_id = str((current_user or {}).get("club_id") or _current_club_id_from_cache())
    profiles = load_player_profiles(club_id)
    cache = load_cache() or {}
    players = cache.get("players") or []
    return {"club_id": club_id, "profiles": profiles, "players_count": len(players)}


@app.put("/api/player-profiles/{player_name}")
def update_player_profile(player_name: str, item: PlayerProfileUpdate, current_user: dict = Depends(get_current_user)):
    """Salva ajuste manual para um jogador sem depender de banco externo."""
    club_id = str((current_user or {}).get("club_id") or _current_club_id_from_cache())
    manual_position = (item.manual_position or "").strip() or None
    archetype = (item.archetype or "").strip() or None
    playstyles = [str(x).strip() for x in (item.playstyles or []) if str(x).strip()][:3]
    notes = (item.notes or "").strip() or None
    try:
        conn = sqlite3.connect(DB_FILE)
        if manual_position or archetype or playstyles or notes:
            conn.execute(
                """
                INSERT INTO player_profiles (club_id, player_name, manual_position, archetype, playstyles, notes, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(club_id, player_name) DO UPDATE SET
                    manual_position=excluded.manual_position,
                    archetype=excluded.archetype,
                    playstyles=excluded.playstyles,
                    notes=excluded.notes,
                    updated_at=CURRENT_TIMESTAMP
                """,
                (club_id, player_name, manual_position, archetype, json.dumps(playstyles, ensure_ascii=False), notes),
            )
        else:
            conn.execute(
                "DELETE FROM player_profiles WHERE club_id=? AND player_name=?",
                (club_id, player_name),
            )
        conn.commit()
        conn.close()
        save_player_profile_supabase(club_id, player_name, manual_position, archetype, playstyles, notes)
    except Exception as e:
        print(f"[profiles] erro ao salvar perfil de {player_name}: {e}")
        raise HTTPException(500, f"Erro ao salvar perfil: {e}")
    return {
        "club_id": club_id,
        "player_name": player_name,
        "manual_position": manual_position,
        "archetype": archetype,
        "playstyles": playstyles,
        "notes": notes,
    }



# ============================================================
# ADVERSARIOS - SCOUT PRE-JOGO
# ============================================================

class OpponentScoutRequest(BaseModel):
    names: List[str]
    platform: str = "auto"


def _num(value, default=0.0):
    try:
        if value is None or value == "":
            return default
        return float(value)
    except Exception:
        return default


def _int_num(value, default=0):
    try:
        if value is None or value == "":
            return default
        return int(float(value))
    except Exception:
        return default


def _light_fetch_opponent_matches(club_id: str, platform: str):
    """Busca leve para scouting de adversario, sem afetar o historico do clube principal."""
    raw = []
    debug = []
    seen = set()
    for mt in ("leagueMatch", "playoffMatch", "friendlyMatch"):
        try:
            data = ea_client.matches(club_id, mt, platform, max_count=20)
            count = len(data) if isinstance(data, list) else 0
            debug.append({"matchType": mt, "count": count, "ok": isinstance(data, list)})
            if isinstance(data, list):
                for item in data:
                    if not isinstance(item, dict):
                        continue
                    key = stable_match_id_for_storage(item, club_id)
                    if key in seen:
                        continue
                    seen.add(key)
                    item["_origin"] = mt
                    raw.append(item)
        except Exception as e:
            debug.append({"matchType": mt, "count": 0, "ok": False, "error": f"{type(e).__name__}: {e}"})
            print(f"[ADVERSARIOS] Erro ao buscar {mt} de {club_id}: {type(e).__name__}: {e}")
    return raw, debug


def _infer_opponent_style(stats: dict, players: list, matches: list):
    gf = _num(stats.get("goals_per_match"), 0)
    ga = round(_num(stats.get("goals_against"), 0) / max(_int_num(stats.get("matches_played"), 0), 1), 2)
    wr = _num(stats.get("win_rate"), 0)
    clean = _int_num(stats.get("clean_sheets"), 0)
    top_goals = sum(_int_num(p.get("goals"), 0) for p in (players or [])[:5])
    top_assists = sum(_int_num(p.get("assists"), 0) for p in (players or [])[:5])
    if gf >= 3.2 and top_goals >= top_assists:
        style = "Ofensivo direto"
    elif top_assists > top_goals and gf >= 2.2:
        style = "Criação e passe"
    elif clean >= max(2, len(matches) // 4) and ga <= 1.5:
        style = "Bloco sólido"
    elif gf >= 2.5 and ga >= 2.5:
        style = "Trocação aberta"
    elif wr >= 60:
        style = "Competitivo equilibrado"
    else:
        style = "Irregular / reativo"
    return style, ga


def _opponent_grade(stats: dict, players: list, goals_against_avg: float):
    wr = _num(stats.get("win_rate"), 0)
    gf = _num(stats.get("goals_per_match"), 0)
    gd = _num(stats.get("goal_diff"), 0) / max(_int_num(stats.get("matches_played"), 0), 1)
    avg_rating = 0
    if players:
        avg_rating = sum(_num(p.get("rating"), 0) for p in players[:8]) / max(len(players[:8]), 1)
    grade = (wr * 0.38) + (min(gf, 5) * 10) + (max(min(gd, 4), -4) * 5) + (avg_rating * 5) - (goals_against_avg * 4)
    grade = max(0, min(100, round(grade, 1)))
    if grade >= 82:
        rank = "S"
    elif grade >= 70:
        rank = "A"
    elif grade >= 58:
        rank = "B"
    elif grade >= 45:
        rank = "C"
    else:
        rank = "D"
    return grade, rank


def _build_opponent_strategy(stats: dict, players: list, style: str, ga_avg: float):
    strengths = []
    weaknesses = []
    strategy = []
    gf = _num(stats.get("goals_per_match"), 0)
    wr = _num(stats.get("win_rate"), 0)
    clean = _int_num(stats.get("clean_sheets"), 0)
    if wr >= 60:
        strengths.append("bom aproveitamento recente")
    if gf >= 3:
        strengths.append("alto volume de gols")
    if clean >= 2:
        strengths.append("consegue jogos sem sofrer gol")
    if players:
        p = players[0]
        strengths.append(f"jogador destaque: {p.get('name')} ({p.get('rating')})")
    if not strengths:
        strengths.append("time ainda sem padrão forte detectado nos dados recentes")
    if ga_avg >= 2.5:
        weaknesses.append("cede muitas chances e sofre muitos gols")
    if gf < 1.5:
        weaknesses.append("baixo poder ofensivo recente")
    if wr < 40:
        weaknesses.append("oscilação de resultados")
    if not weaknesses:
        weaknesses.append("não há fraqueza gritante nos dados recentes")
    if style in ("Ofensivo direto", "Trocação aberta"):
        strategy.append("evitar perder bola no meio e atacar o espaço nas costas")
        strategy.append("baixar a exposição dos zagueiros nos primeiros minutos")
    elif style == "Bloco sólido":
        strategy.append("circular a bola com paciência e buscar inversões rápidas")
        strategy.append("forçar finalizações de média distância e rebotes")
    elif style == "Criação e passe":
        strategy.append("pressionar o armador e cortar linhas de passe por dentro")
        strategy.append("não deixar o meia receber de frente")
    else:
        strategy.append("começar pressionando para testar a saída de bola")
        strategy.append("manter posse e atrair erro antes de acelerar")
    return strengths, weaknesses, "; ".join(strategy) + "."


def build_opponent_scout(club_name: str, platform: str = "auto"):
    name = (club_name or "").strip()
    if not name:
        return {"name": club_name, "found": False, "error": "Nome vazio"}
    try:
        search = ea_client.search_club(name, platform)
        if not search.get("success"):
            print(f"[ADVERSARIOS] Clube nao encontrado: {name}")
            return {"name": name, "found": False, "error": "Clube nao encontrado"}
        club_id = str(search.get("clubId"))
        plat = search.get("platform") or platform or "common-gen5"
        real_name = search.get("name") or name
        overall = ea_client.overall_stats(club_id, plat)
        members = ea_client.members(club_id, plat)
        players = parse_players(members)[:5]
        raw_matches, debug = _light_fetch_opponent_matches(club_id, plat)
        matches = parse_matches(raw_matches, club_id)
        stats = calc_club_stats(overall, {}, matches)
        if not stats.get("matches_played") and matches:
            stats = calc_club_stats([], {}, matches)
        style, ga_avg = _infer_opponent_style(stats, players, matches)
        grade, rank = _opponent_grade(stats, players, ga_avg)
        strengths, weaknesses, strategy = _build_opponent_strategy(stats, players, style, ga_avg)
        return {
            "name": real_name,
            "searched_name": name,
            "found": True,
            "club_id": club_id,
            "platform": plat,
            "grade": grade,
            "rank": rank,
            "style": style,
            "stats": {
                "matches": _int_num(stats.get("matches_played"), len(matches)),
                "wins": _int_num(stats.get("wins"), 0),
                "draws": _int_num(stats.get("draws"), 0),
                "losses": _int_num(stats.get("losses"), 0),
                "win_rate": _num(stats.get("win_rate"), 0),
                "goals_for": _int_num(stats.get("goals_for"), 0),
                "goals_against": _int_num(stats.get("goals_against"), 0),
                "goal_diff": _int_num(stats.get("goal_diff"), 0),
                "goals_per_match": _num(stats.get("goals_per_match"), 0),
                "goals_against_per_match": ga_avg,
                "clean_sheets": _int_num(stats.get("clean_sheets"), 0),
            },
            "top_players": players,
            "strengths": strengths,
            "weaknesses": weaknesses,
            "strategy": strategy,
            "debug": debug,
        }
    except Exception as e:
        print(f"[ADVERSARIOS] Erro ao analisar {name}: {type(e).__name__}: {e}")
        return {"name": name, "found": False, "error": f"{type(e).__name__}: {e}"}


@app.post("/api/opponents/scout")
def scout_opponents(req: OpponentScoutRequest, current_user: dict = Depends(require_admin)):
    names = []
    seen = set()
    for raw in req.names or []:
        nm = (raw or "").strip()
        if not nm:
            continue
        key = nm.lower()
        if key in seen:
            continue
        seen.add(key)
        names.append(nm)
    names = names[:5]
    if not names:
        raise HTTPException(400, "Informe pelo menos um clube adversario")
    results = [build_opponent_scout(nm, req.platform) for nm in names]
    return {"count": len(results), "opponents": results}

# ============================================================
# AGENDA - CRUD
# ============================================================

class AgendaItem(BaseModel):
    opponent: str
    match_date: str  # YYYY-MM-DD
    match_time: Optional[str] = None  # HH:MM
    match_type: str = "liga"  # liga | copa | amistoso
    location: Optional[str] = None
    notes: Optional[str] = None


@app.get("/api/agenda")
def list_agenda(current_user: dict = Depends(get_current_user)):
    club_id = _current_club_id_from_cache()
    sb = get_supabase()
    if sb:
        try:
            resp = (
                sb.table("agenda")
                .select("id,opponent,match_date,match_time,match_type,location,notes")
                .or_(f"club_id.is.null,club_id.eq.{club_id}")
                .order("match_date", desc=False)
                .order("match_time", desc=False)
                .execute()
            )
            rows = getattr(resp, "data", None) or []
            return rows
        except Exception as e:
            print(f"[SUPABASE] Aviso ao listar agenda: {type(e).__name__}: {e}")
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT id, opponent, match_date, match_time, match_type, location, notes "
        "FROM agenda ORDER BY match_date ASC, match_time ASC"
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


@app.post("/api/agenda")
def create_agenda(item: AgendaItem, current_user: dict = Depends(get_current_user)):
    club_id = _current_club_id_from_cache()
    sb = get_supabase()
    payload = {"club_id": club_id, **item.dict()}
    if sb:
        try:
            resp = sb.table("agenda").insert(payload).execute()
            rows = getattr(resp, "data", None) or []
            if rows:
                return rows[0]
        except Exception as e:
            print(f"[SUPABASE] Aviso ao criar agenda: {type(e).__name__}: {e}")
    conn = sqlite3.connect(DB_FILE)
    cur = conn.execute(
        "INSERT INTO agenda (opponent, match_date, match_time, match_type, location, notes) "
        "VALUES (?,?,?,?,?,?)",
        (item.opponent, item.match_date, item.match_time, item.match_type, item.location, item.notes)
    )
    new_id = cur.lastrowid
    conn.commit()
    conn.close()
    return {"id": new_id, **item.dict()}


@app.put("/api/agenda/{item_id}")
def update_agenda(item_id: int, item: AgendaItem, current_user: dict = Depends(get_current_user)):
    club_id = _current_club_id_from_cache()
    sb = get_supabase()
    payload = {"club_id": club_id, **item.dict(), "updated_at": _now_iso()}
    if sb:
        try:
            resp = sb.table("agenda").update(payload).eq("id", item_id).eq("club_id", club_id).execute()
            rows = getattr(resp, "data", None) or []
            if rows:
                return rows[0]
        except Exception as e:
            print(f"[SUPABASE] Aviso ao atualizar agenda: {type(e).__name__}: {e}")
    conn = sqlite3.connect(DB_FILE)
    res = conn.execute(
        "UPDATE agenda SET opponent=?, match_date=?, match_time=?, match_type=?, location=?, notes=? WHERE id=?",
        (item.opponent, item.match_date, item.match_time, item.match_type, item.location, item.notes, item_id)
    )
    if res.rowcount == 0:
        conn.close()
        raise HTTPException(404, "Agendamento nao encontrado")
    conn.commit()
    conn.close()
    return {"id": item_id, **item.dict()}


@app.delete("/api/agenda/{item_id}")
def delete_agenda(item_id: int, current_user: dict = Depends(get_current_user)):
    sb = get_supabase()
    if sb:
        try:
            sb.table("agenda").delete().eq("id", item_id).eq("club_id", club_id).execute()
            return {"deleted": item_id}
        except Exception as e:
            print(f"[SUPABASE] Aviso ao excluir agenda: {type(e).__name__}: {e}")
    conn = sqlite3.connect(DB_FILE)
    res = conn.execute("DELETE FROM agenda WHERE id=?", (item_id,))
    if res.rowcount == 0:
        conn.close()
        raise HTTPException(404, "Agendamento nao encontrado")
    conn.commit()
    conn.close()
    return {"deleted": item_id}

# ============================================================
# FRONTEND HTML
# ============================================================

@app.get("/", response_class=HTMLResponse)
def home():
    return HTMLResponse(content=render_html())


def render_html() -> str:
    return r"""<!DOCTYPE html>
<html lang="pt-BR">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Scout Clubs Pro - Análise EA FC</title>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800;900&family=JetBrains+Mono:wght@500;700&display=swap" rel="stylesheet">
<script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
<style>
:root {
  --green: #00FF73;
  --green-dim: #00ff7333;
  --green-glow: #00ff7366;
  --bg: #000000;
  --bg-2: #0a0a0a;
  --bg-3: #111111;
  --bg-card: #0d0d0d;
  --border: #1a1a1a;
  --border-2: #222;
  --text: #ffffff;
  --text-2: #888;
  --text-3: #555;
  --red: #ff3344;
  --yellow: #ffaa00;
}

* { box-sizing: border-box; margin: 0; padding: 0; }

body {
  font-family: 'Inter', sans-serif;
  background: var(--bg);
  color: var(--text);
  min-height: 100vh;
  overflow-x: hidden;
}

/* HEADER */
.header {
  padding: 16px 20px;
  border-bottom: 1px solid var(--border);
  background: var(--bg);
  position: relative;
  z-index: 50;
}

.header-inner {
  max-width: 1400px;
  margin: 0 auto;
  display: flex;
  justify-content: space-between;
  align-items: center;
}

.logo {
  display: flex;
  align-items: center;
  gap: 10px;
}

.logo-icon {
  width: 40px;
  height: 40px;
  border: 2px solid var(--green);
  border-radius: 50%;
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 20px;
}

.logo-text {
  font-size: 18px;
  font-weight: 800;
  letter-spacing: 1px;
}

.logo-text span {
  color: var(--green);
}

.logo-sub {
  font-size: 9px;
  color: var(--text-3);
  letter-spacing: 3px;
  text-transform: uppercase;
}

.btn-sync {
  background: var(--bg-3);
  border: 1px solid var(--border-2);
  color: var(--green);
  padding: 8px 14px;
  border-radius: 8px;
  cursor: pointer;
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 13px;
  transition: all 0.2s;
}

.btn-sync:hover {
  background: var(--green-dim);
  border-color: var(--green);
}

/* CLUB CARD */
.club-card {
  max-width: 1400px;
  margin: 20px auto;
  padding: 0 20px;
}

.club-info {
  background: var(--bg-card);
  border: 1px solid var(--border);
  border-radius: 16px;
  padding: 20px;
  display: flex;
  align-items: center;
  gap: 16px;
}

.club-shield {
  width: 60px;
  height: 60px;
  background: var(--bg-3);
  border-radius: 12px;
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 28px;
  border: 1px solid var(--border-2);
}

.club-name {
  font-size: 22px;
  font-weight: 800;
  letter-spacing: 1px;
}

.club-meta {
  font-size: 12px;
  color: var(--text-2);
  margin-top: 2px;
}

/* TABS */
.tabs {
  max-width: 1400px;
  margin: 0 auto;
  padding: 10px 20px;
  display: flex;
  gap: 8px;
  overflow-x: auto;
  scrollbar-width: none;
  border: 1px solid var(--border);
  border-radius: 50px;
  margin: 20px 20px;
  background: var(--bg-card);
}

.tabs::-webkit-scrollbar { display: none; }

.tab {
  padding: 10px 18px;
  border-radius: 30px;
  font-size: 11px;
  font-weight: 700;
  letter-spacing: 1.5px;
  cursor: pointer;
  white-space: nowrap;
  transition: all 0.2s;
  color: var(--text-3);
  text-transform: uppercase;
}

.tab.active {
  background: var(--green-dim);
  color: var(--green);
  text-shadow: 0 0 10px var(--green);
}

.tab:hover:not(.active) {
  color: var(--text);
}

/* PERIOD FILTER */
.period-filter {
  max-width: 1400px;
  margin: 0 auto 20px;
  padding: 0 20px;
  display: flex;
  gap: 10px;
}

.period {
  padding: 6px 16px;
  border-radius: 20px;
  font-size: 11px;
  font-weight: 700;
  letter-spacing: 1.5px;
  border: 1px solid var(--border-2);
  background: transparent;
  color: var(--text-3);
  cursor: pointer;
  transition: all 0.2s;
}

.period.active {
  border-color: var(--green);
  background: var(--green-dim);
  color: var(--green);
}

/* CONTAINER */
.container {
  max-width: 1400px;
  margin: 0 auto;
  padding: 0 20px 40px;
}

.tab-content {
  display: none;
}

.tab-content.active {
  display: block;
}

/* STATS GRID */
.stats-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
  gap: 12px;
  margin-bottom: 20px;
}

.stat-card {
  background: var(--bg-card);
  border: 1px solid var(--border);
  border-radius: 16px;
  padding: 24px 16px;
  text-align: center;
  position: relative;
  overflow: hidden;
  transition: all 0.3s;
}

.stat-card:hover {
  border-color: var(--green);
  transform: translateY(-2px);
}

.stat-card.highlight {
  border-color: var(--green);
  background: linear-gradient(180deg, var(--green-dim) 0%, transparent 100%);
}

.stat-value {
  font-size: 38px;
  font-weight: 800;
  letter-spacing: -1px;
  line-height: 1;
  margin-bottom: 8px;
}

.stat-value.green { color: var(--green); }
.stat-value.red { color: var(--red); }
.stat-value.yellow { color: var(--yellow); }
.stat-value.compound {
  font-size: 32px;
  display: flex;
  justify-content: center;
  gap: 6px;
}

.stat-label {
  font-size: 10px;
  color: var(--text-2);
  letter-spacing: 2px;
  text-transform: uppercase;
}

/* CIRCULAR CHARTS */
.circles-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
  gap: 12px;
  margin-bottom: 20px;
}

.circle-card {
  background: var(--bg-card);
  border: 1px solid var(--border);
  border-radius: 16px;
  padding: 20px;
  display: flex;
  flex-direction: column;
  align-items: center;
}

.circle-svg {
  width: 130px;
  height: 130px;
  position: relative;
}

.circle-svg svg {
  transform: rotate(-90deg);
}

.circle-bg {
  fill: none;
  stroke: var(--border-2);
  stroke-width: 8;
}

.circle-progress {
  fill: none;
  stroke: var(--green);
  stroke-width: 8;
  stroke-linecap: round;
  filter: drop-shadow(0 0 8px var(--green));
  transition: stroke-dasharray 1s ease;
}

.circle-text {
  position: absolute;
  top: 50%;
  left: 50%;
  transform: translate(-50%, -50%);
  font-size: 22px;
  font-weight: 800;
  color: var(--green);
}

.circle-label {
  margin-top: 12px;
  font-size: 11px;
  color: var(--text-2);
  letter-spacing: 2px;
  text-transform: uppercase;
}

/* MVP CARD */
.mvp-card {
  background: var(--bg-card);
  border: 1px solid var(--green);
  border-radius: 16px;
  padding: 24px;
  display: flex;
  align-items: center;
  gap: 24px;
  margin-bottom: 20px;
  position: relative;
  overflow: hidden;
}

.mvp-card::before {
  content: '';
  position: absolute;
  top: 0; left: 0; right: 0;
  height: 1px;
  background: linear-gradient(90deg, transparent, var(--green), transparent);
}

.mvp-rating {
  font-size: 56px;
  font-weight: 900;
  color: var(--green);
  text-shadow: 0 0 20px var(--green);
  line-height: 1;
}

.mvp-info { flex: 1; }
.mvp-name {
  font-size: 22px;
  font-weight: 800;
  letter-spacing: 1px;
}
.mvp-meta {
  font-size: 11px;
  color: var(--text-2);
  margin-top: 4px;
  letter-spacing: 1px;
}

.mvp-stats {
  display: flex;
  gap: 20px;
  margin-top: 12px;
}

.mvp-stat {
  text-align: center;
}

.mvp-stat-value {
  font-size: 18px;
  font-weight: 700;
}

.mvp-stat-label {
  font-size: 9px;
  color: var(--text-2);
  letter-spacing: 1.5px;
  margin-top: 2px;
  text-transform: uppercase;
}

.mvp-badge {
  position: absolute;
  top: 12px;
  right: 16px;
  font-size: 10px;
  letter-spacing: 3px;
  color: var(--green);
}

/* SECTION TITLE */
.section-title {
  font-size: 12px;
  font-weight: 700;
  color: var(--green);
  letter-spacing: 3px;
  text-transform: uppercase;
  margin: 30px 0 14px;
}

/* OPPONENTS LIST */
.opponents-list {
  background: var(--bg-card);
  border: 1px solid var(--border);
  border-radius: 16px;
  overflow: hidden;
}

.opp-row {
  padding: 14px 20px;
  display: flex;
  justify-content: space-between;
  align-items: center;
  border-bottom: 1px solid var(--border);
}

.opp-row:last-child { border-bottom: none; }

.opp-name {
  font-weight: 700;
  letter-spacing: 1px;
}

.opp-name-small {
  color: var(--text-3);
  font-size: 11px;
  margin-left: 6px;
}

.opp-stats {
  display: flex;
  gap: 30px;
}

.opp-stat {
  display: flex;
  align-items: baseline;
  gap: 4px;
}

.opp-stat-val {
  font-size: 16px;
  font-weight: 700;
}
.opp-stat-val.green { color: var(--green); }
.opp-stat-val.red { color: var(--red); }
.opp-stat-label {
  font-size: 9px;
  color: var(--text-3);
  letter-spacing: 1px;
}

/* PERFORMANCE BARS */
.perf-bars {
  display: flex;
  gap: 8px;
  align-items: flex-end;
  background: var(--bg-card);
  border: 1px solid var(--border);
  border-radius: 16px;
  padding: 24px 16px;
  height: 200px;
  overflow-x: auto;
}

.perf-bar {
  display: flex;
  flex-direction: column;
  align-items: center;
  min-width: 50px;
  height: 100%;
  justify-content: flex-end;
}

.perf-bar-bar {
  width: 36px;
  border-radius: 8px 8px 4px 4px;
  margin-bottom: 8px;
  transition: all 0.3s;
}

.perf-bar.win .perf-bar-bar { background: var(--green); box-shadow: 0 0 10px var(--green); }
.perf-bar.draw .perf-bar-bar { background: var(--yellow); }
.perf-bar.loss .perf-bar-bar { background: var(--red); }

.perf-bar-score {
  font-size: 10px;
  font-weight: 700;
  letter-spacing: 1px;
}

.perf-bar-date {
  font-size: 8px;
  color: var(--text-3);
  margin-top: 2px;
}

/* PLAYERS GRID */
.players-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(280px, 1fr));
  gap: 12px;
}

.player-card {
  background: var(--bg-card);
  border: 1px solid var(--border);
  border-radius: 16px;
  padding: 20px;
  cursor: pointer;
  transition: all 0.2s;
}

.player-card:hover {
  border-color: var(--green);
  transform: translateY(-2px);
}

.player-rating-big {
  font-size: 38px;
  font-weight: 800;
  color: var(--green);
  text-align: center;
  text-shadow: 0 0 15px var(--green);
  line-height: 1;
}

.player-pos {
  text-align: center;
  margin: 6px 0;
}

.player-pos-badge {
  display: inline-block;
  background: var(--green-dim);
  color: var(--green);
  padding: 3px 10px;
  border-radius: 4px;
  font-size: 9px;
  font-weight: 700;
  letter-spacing: 1.5px;
}

.player-name {
  text-align: center;
  font-size: 16px;
  font-weight: 700;
  letter-spacing: 1px;
  margin: 8px 0 12px;
  text-transform: uppercase;
}

.player-stats {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 6px;
  border-top: 1px solid var(--border);
  padding-top: 12px;
}

.player-stat {
  display: flex;
  justify-content: space-between;
  font-size: 11px;
}

.player-stat-label {
  color: var(--text-3);
}

.player-stat-val {
  color: var(--green);
  font-weight: 700;
}

/* MATCHES LIST */
.matches-list {
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.match-card {
  background: var(--bg-card);
  border: 1px solid var(--border);
  border-radius: 12px;
  padding: 14px 18px;
  display: flex;
  justify-content: space-between;
  align-items: center;
  cursor: pointer;
  transition: all 0.2s;
}

.match-card:hover {
  border-color: var(--green);
}

.match-opp {
  font-weight: 700;
  letter-spacing: 1px;
  font-size: 14px;
}

.match-mom {
  font-size: 10px;
  color: var(--text-2);
  margin-top: 4px;
  letter-spacing: 1px;
}

.match-mom strong {
  color: var(--green);
}

.match-results {
  display: flex;
  gap: 10px;
  align-items: center;
}

.match-badge {
  padding: 4px 10px;
  border-radius: 6px;
  font-size: 11px;
  font-weight: 700;
  letter-spacing: 1px;
  border: 1px solid;
}

.match-badge.v {
  color: var(--green);
  border-color: var(--green-dim);
  background: var(--green-dim);
}

.match-badge.e {
  color: var(--yellow);
  border-color: rgba(255,170,0,0.3);
  background: rgba(255,170,0,0.1);
}

.match-badge.d {
  color: var(--red);
  border-color: rgba(255,51,68,0.3);
  background: rgba(255,51,68,0.1);
}

.match-score {
  font-size: 16px;
  font-weight: 700;
  color: var(--text);
}

/* FORMATION FIELD */
.formation-wrapper {
  background: var(--bg-card);
  border: 1px solid var(--border);
  border-radius: 16px;
  padding: 20px;
  margin-bottom: 20px;
}

.formation-title {
  text-align: center;
  font-size: 14px;
  font-weight: 700;
  letter-spacing: 3px;
  color: var(--green);
  margin-bottom: 16px;
  text-transform: uppercase;
}

.field {
  position: relative;
  background:
    radial-gradient(ellipse at center, rgba(0,255,115,0.05) 0%, transparent 70%),
    linear-gradient(180deg, #001a0d 0%, #000 100%);
  border: 2px solid var(--green-dim);
  border-radius: 12px;
  height: 600px;
  margin: 0 auto;
  max-width: 480px;
  overflow: hidden;
}

.field::before, .field::after {
  content: '';
  position: absolute;
  border: 1px solid var(--green-dim);
  left: 50%;
  transform: translateX(-50%);
  width: 60%;
}

.field::before {
  top: 0;
  height: 80px;
  border-top: none;
  border-radius: 0 0 8px 8px;
}

.field::after {
  bottom: 0;
  height: 80px;
  border-bottom: none;
  border-radius: 8px 8px 0 0;
}

.field-line {
  position: absolute;
  top: 50%;
  left: 0;
  right: 0;
  height: 1px;
  background: var(--green-dim);
}

.field-circle {
  position: absolute;
  top: 50%;
  left: 50%;
  transform: translate(-50%, -50%);
  width: 80px;
  height: 80px;
  border: 1px solid var(--green-dim);
  border-radius: 50%;
}

.player-on-field {
  position: absolute;
  transform: translate(-50%, -50%);
  display: flex;
  flex-direction: column;
  align-items: center;
}

.player-circle {
  width: 50px;
  height: 50px;
  border: 2px solid var(--green);
  border-radius: 50%;
  background: rgba(0,0,0,0.7);
  display: flex;
  align-items: center;
  justify-content: center;
  color: var(--green);
  font-size: 14px;
  font-weight: 700;
  text-shadow: 0 0 8px var(--green);
  box-shadow: 0 0 15px var(--green-glow);
}

.player-circle-name {
  font-size: 9px;
  color: var(--text);
  margin-top: 4px;
  text-align: center;
  background: rgba(0,0,0,0.6);
  padding: 1px 6px;
  border-radius: 4px;
  white-space: nowrap;
  letter-spacing: 0.5px;
}

.formation-label {
  text-align: center;
  margin-top: 16px;
  color: var(--text-2);
  font-size: 11px;
  letter-spacing: 2px;
}

/* MODAL */
.modal-bg {
  display: none;
  position: fixed;
  inset: 0;
  background: rgba(0,0,0,0.85);
  z-index: 1000;
  backdrop-filter: blur(8px);
  animation: fadeIn 0.2s;
}

.modal-bg.active { display: flex; align-items: center; justify-content: center; padding: 20px; }

.modal-box {
  background: var(--bg-2);
  border: 1px solid var(--green-dim);
  border-radius: 16px;
  padding: 28px;
  max-width: 980px;
  width: 100%;
  max-height: 85vh;
  overflow-y: auto;
  overflow-x: hidden;
  box-shadow: 0 0 40px var(--green-dim);
  animation: slideUp 0.3s;
}

.modal-close {
  float: right;
  background: var(--bg-3);
  border: 1px solid var(--border-2);
  color: var(--text);
  width: 32px;
  height: 32px;
  border-radius: 50%;
  cursor: pointer;
  font-size: 18px;
}

.modal-content {
  margin-top: 10px;
  line-height: 1.7;
}

.modal-content h2 { font-size: 18px; color: var(--green); margin: 12px 0 8px; }
.modal-content h3 { font-size: 15px; margin: 14px 0 6px; color: var(--green); }
.modal-content p { margin-bottom: 10px; color: var(--text-2); }
.modal-content strong { color: var(--green); }
.modal-content ul { padding-left: 20px; margin-bottom: 10px; }
.modal-content li { color: var(--text-2); margin-bottom: 4px; }

@keyframes fadeIn { from { opacity: 0; } to { opacity: 1; } }
@keyframes slideUp { from { transform: translateY(20px); opacity: 0; } to { transform: translateY(0); opacity: 1; } }

/* SYNC PROGRESS */
.sync-progress {
  display: none;
  background: var(--bg-card);
  border: 2px solid var(--green);
  border-radius: 16px;
  padding: 24px;
  margin: 20px;
  box-shadow: 0 0 30px var(--green-glow);
}

.sync-progress.active { display: block; }

.sync-title {
  font-size: 15px;
  font-weight: 700;
  color: var(--green);
  margin-bottom: 4px;
  letter-spacing: 1px;
}

.sync-step {
  font-size: 11px;
  color: var(--text-2);
  margin-bottom: 14px;
  letter-spacing: 1px;
}

.sync-progress-bar {
  height: 6px;
  background: var(--border-2);
  border-radius: 3px;
  overflow: hidden;
  margin-bottom: 14px;
}

.sync-progress-fill {
  height: 100%;
  background: linear-gradient(90deg, var(--green), #00cc5c);
  border-radius: 3px;
  width: 0%;
  transition: width 0.4s;
  box-shadow: 0 0 10px var(--green);
}

.sync-log {
  background: #000;
  border: 1px solid var(--border);
  border-radius: 8px;
  padding: 14px;
  max-height: 200px;
  overflow-y: auto;
  overflow-x: hidden;
  font-family: 'JetBrains Mono', monospace;
  font-size: 11px;
  line-height: 1.5;
}

.sync-log-line {
  color: var(--green);
  margin-bottom: 2px;
}

/* EMPTY STATE */
.empty-state {
  text-align: center;
  padding: 100px 20px;
}

.empty-icon {
  font-size: 80px;
  margin-bottom: 20px;
  filter: drop-shadow(0 0 15px var(--green));
}

.empty-title {
  font-size: 24px;
  font-weight: 700;
  margin-bottom: 8px;
}

.empty-text {
  color: var(--text-2);
  margin-bottom: 20px;
}

.btn-primary {
  background: var(--green);
  color: #000;
  border: none;
  padding: 12px 24px;
  border-radius: 8px;
  font-weight: 700;
  cursor: pointer;
  font-size: 14px;
  letter-spacing: 1px;
  text-transform: uppercase;
  box-shadow: 0 0 20px var(--green-glow);
}

/* LOADING */
.loading {
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 60px 20px;
  color: var(--text-2);
}

.spinner {
  width: 40px;
  height: 40px;
  border: 3px solid var(--border-2);
  border-top-color: var(--green);
  border-radius: 50%;
  animation: spin 0.8s linear infinite;
  margin-right: 16px;
}

@keyframes spin { to { transform: rotate(360deg); } }

/* CONFRONTS */
.confronts-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(300px, 1fr));
  gap: 12px;
}

.confront-card {
  background: var(--bg-card);
  border: 1px solid var(--border);
  border-radius: 12px;
  padding: 16px;
  display: flex;
  justify-content: space-between;
  align-items: center;
}

.confront-name {
  font-weight: 700;
  letter-spacing: 1px;
  font-size: 14px;
}

.confront-vs {
  display: flex;
  gap: 8px;
}

.vs-tag {
  padding: 4px 10px;
  border-radius: 6px;
  font-size: 11px;
  font-weight: 700;
  letter-spacing: 1px;
  border: 1px solid;
}

.vs-tag.v { color: var(--green); border-color: var(--green-dim); background: var(--green-dim); }
.vs-tag.e { color: var(--yellow); border-color: rgba(255,170,0,0.3); background: rgba(255,170,0,0.1); }
.vs-tag.d { color: var(--red); border-color: rgba(255,51,68,0.3); background: rgba(255,51,68,0.1); }

/* COMPARAR */
.compare-grid {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 24px;
  margin-top: 16px;
}
.compare-pick {
  background: var(--bg-card);
  border: 1px solid var(--border);
  border-radius: 12px;
  padding: 16px;
}
.compare-pick select {
  width: 100%;
  padding: 10px 12px;
  background: var(--bg);
  color: var(--text);
  border: 1px solid var(--border);
  border-radius: 8px;
  margin-bottom: 12px;
  font-size: 14px;
}
.compare-bars {
  background: var(--bg-card);
  border: 1px solid var(--border);
  border-radius: 12px;
  padding: 18px;
  margin-top: 18px;
}
.compare-bar-row {
  display: grid;
  grid-template-columns: 80px 1fr 100px 1fr 80px;
  align-items: center;
  gap: 8px;
  margin: 10px 0;
  font-size: 12px;
}
.compare-bar-label {
  text-align: center;
  text-transform: uppercase;
  letter-spacing: 1px;
  color: var(--text-2);
  font-size: 11px;
}
.compare-bar {
  height: 16px;
  background: rgba(0,255,115,0.08);
  border-radius: 4px;
  position: relative;
  overflow: hidden;
}
.compare-bar.left .fill { right: 0; }
.compare-bar.right .fill { left: 0; }
.compare-bar .fill {
  position: absolute;
  top: 0;
  bottom: 0;
  background: var(--green);
  box-shadow: 0 0 8px var(--green-glow);
}
.compare-bar-val {
  font-weight: 700;
  color: var(--text);
}
.compare-bar-val.left { text-align: right; }
.compare-bar-val.right { text-align: left; }

/* PLAYER DETAIL MODAL */
.player-detail h2 { margin: 0 0 4px; }
.player-detail .pos-tag {
  display: inline-block;
  padding: 4px 10px;
  background: var(--green-dim);
  color: var(--green);
  border-radius: 6px;
  font-size: 11px;
  letter-spacing: 1px;
  text-transform: uppercase;
  font-weight: 700;
}
.detail-grid {
  display: grid;
  grid-template-columns: repeat(4, 1fr);
  gap: 10px;
  margin: 16px 0 24px;
}
.detail-stat {
  background: rgba(0,255,115,0.05);
  border: 1px solid var(--border);
  border-radius: 10px;
  padding: 10px;
  text-align: center;
}
.detail-stat .v { font-size: 20px; font-weight: 700; color: var(--green); }
.detail-stat .l { font-size: 10px; text-transform: uppercase; color: var(--text-2); letter-spacing: 1px; }

.history-table {
  width: 100%;
  border-collapse: collapse;
  font-size: 12px;
  margin-top: 8px;
}
.history-table th {
  text-align: left;
  padding: 8px 6px;
  border-bottom: 1px solid var(--border);
  color: var(--text-2);
  text-transform: uppercase;
  font-size: 10px;
  letter-spacing: 1px;
}
.history-table td {
  padding: 8px 6px;
  border-bottom: 1px solid rgba(255,255,255,0.04);
}
.history-table tr:hover { background: rgba(0,255,115,0.04); }
.tag {
  display: inline-block;
  padding: 2px 8px;
  border-radius: 4px;
  font-size: 10px;
  font-weight: 700;
  letter-spacing: 1px;
  text-transform: uppercase;
}
.tag.liga { background: rgba(0,255,115,0.1); color: var(--green); border: 1px solid var(--green-dim); }
.tag.copa { background: rgba(255,170,0,0.1); color: var(--yellow); border: 1px solid rgba(255,170,0,0.3); }
.tag.amistoso { background: rgba(120,120,120,0.15); color: var(--text-2); border: 1px solid var(--border); }
.tag.v { background: rgba(0,255,115,0.15); color: var(--green); }
.tag.e { background: rgba(255,170,0,0.15); color: var(--yellow); }
.tag.d { background: rgba(255,51,68,0.15); color: var(--red); }
.sofi {
  font-weight: 800;
  font-size: 14px;
  color: var(--green);
}

.analytics-hero {
  display: grid;
  grid-template-columns: 1fr auto;
  gap: 18px;
  align-items: center;
  padding-bottom: 18px;
  border-bottom: 1px solid var(--border);
}
.analytics-score {
  width: 104px;
  height: 104px;
  border: 2px solid var(--green);
  border-radius: 50%;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  color: var(--green);
  box-shadow: 0 0 20px var(--green-glow);
}
.analytics-score .num { font-size: 30px; font-weight: 900; line-height: 1; }
.analytics-score .lab { font-size: 9px; letter-spacing: 1px; text-transform: uppercase; color: var(--text-2); }
.analytics-cards {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(108px, 1fr));
  gap: 8px;
  margin: 16px 0;
}
.analytics-card {
  background: rgba(0,255,115,0.05);
  border: 1px solid var(--border);
  border-radius: 10px;
  padding: 10px;
  text-align: center;
}
.analytics-card .v { font-size: 19px; font-weight: 800; color: var(--green); }
.analytics-card .l { font-size: 9px; text-transform: uppercase; color: var(--text-2); letter-spacing: 1px; margin-top: 2px; }
.analytics-grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 12px;
  margin: 16px 0;
  width: 100%;
  overflow: hidden;
}
.chart-box {
  background: var(--bg-card);
  border: 1px solid var(--border);
  border-radius: 12px;
  padding: 12px;
  height: 260px;
  min-height: 260px;
  max-height: 260px;
  overflow: hidden;
  position: relative;
}
.chart-box.wide {
  grid-column: span 2;
  height: 360px;
  min-height: 360px;
  max-height: 360px;
}
.chart-box canvas {
  display: block;
  width: 100% !important;
  height: 210px !important;
  max-height: 210px !important;
}
.chart-box.wide canvas {
  height: 310px !important;
  max-height: 310px !important;
}
.chart-title {
  font-size: 10px;
  color: var(--green);
  letter-spacing: 2px;
  text-transform: uppercase;
  margin-bottom: 8px;
}
.heatmap-wrap {
  display: grid;
  grid-template-columns: 260px 1fr;
  gap: 14px;
  align-items: stretch;
}
.heatmap-field {
  display: grid;
  grid-template-columns: repeat(3, 1fr);
  grid-template-rows: repeat(3, 1fr);
  height: 360px;
  border: 2px solid var(--green-dim);
  border-radius: 12px;
  overflow: hidden;
  background: linear-gradient(180deg, #001a0d, #000);
}
.heat-zone {
  border: 1px solid rgba(0,255,115,0.14);
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 9px;
  text-transform: uppercase;
  letter-spacing: 1px;
  color: rgba(255,255,255,0.62);
}
.analytics-note {
  color: var(--text-2);
  font-size: 12px;
  line-height: 1.6;
}
.mini-insights {
  display: grid;
  gap: 8px;
}
.mini-insight {
  border: 1px solid var(--border);
  border-radius: 10px;
  padding: 10px;
  background: rgba(255,255,255,0.02);
}
.mini-insight .k { color: var(--text-2); font-size: 10px; text-transform: uppercase; letter-spacing: 1px; }
.mini-insight .v { color: var(--text); font-weight: 700; margin-top: 3px; }


/* OPPONENT SCOUT */
.opponent-form { background: var(--bg-card); border: 1px solid var(--border); border-radius: 12px; padding: 16px; margin-bottom: 18px; display: grid; grid-template-columns: repeat(5, 1fr); gap: 10px; }
.opponent-form input { background: var(--bg); color: var(--text); border: 1px solid var(--border); border-radius: 8px; padding: 11px 12px; font-size: 13px; font-family: inherit; min-width: 0; }
.opponent-form .full { grid-column: 1 / -1; display:flex; justify-content:flex-end; gap:10px; flex-wrap:wrap; }
.opponent-results { display:grid; gap:14px; }
.opponent-card { background: var(--bg-card); border: 1px solid var(--border); border-radius: 12px; padding: 16px; }
.opponent-head { display:flex; align-items:flex-start; justify-content:space-between; gap:12px; margin-bottom:12px; }
.opponent-name { font-size:20px; font-weight:900; letter-spacing:.5px; }
.opponent-style { color:var(--text-2); font-size:12px; margin-top:3px; }
.opponent-grade { min-width:74px; height:74px; border:2px solid var(--green); border-radius:50%; display:flex; flex-direction:column; align-items:center; justify-content:center; color:var(--green); box-shadow:0 0 18px var(--green-dim); }
.opponent-grade .rank { font-size:24px; font-weight:900; line-height:1; }
.opponent-grade .score { font-size:10px; color:var(--text-2); letter-spacing:1px; margin-top:3px; }
.opponent-table-wrap { overflow-x:auto; border:1px solid var(--border); border-radius:10px; margin:12px 0; }
.opponent-table { width:100%; min-width:760px; border-collapse:collapse; font-size:12px; }
.opponent-table th { text-align:left; color:var(--text-2); font-size:10px; letter-spacing:1.5px; text-transform:uppercase; padding:9px 10px; border-bottom:1px solid var(--border); }
.opponent-table td { padding:10px; border-bottom:1px solid rgba(255,255,255,0.04); vertical-align:top; }
.opponent-table tr:last-child td { border-bottom:none; }
.scout-pill { display:inline-block; padding:3px 8px; border-radius:999px; background:var(--green-dim); color:var(--green); border:1px solid rgba(0,255,115,.25); font-size:10px; font-weight:800; letter-spacing:1px; text-transform:uppercase; }
.scout-cols { display:grid; grid-template-columns:1fr 1fr 1.4fr; gap:12px; }
.scout-box { background:rgba(255,255,255,0.025); border:1px solid var(--border); border-radius:10px; padding:12px; min-height:96px; }
.scout-box h4 { color:var(--green); font-size:11px; text-transform:uppercase; letter-spacing:2px; margin-bottom:8px; }
.scout-box ul { padding-left:17px; color:var(--text-2); font-size:12px; line-height:1.5; }
.scout-box p { color:var(--text-2); font-size:12px; line-height:1.55; }
@media (max-width: 900px) { .opponent-form { grid-template-columns:1fr 1fr; } .scout-cols { grid-template-columns:1fr; } }
@media (max-width: 560px) { .opponent-form { grid-template-columns:1fr; } .opponent-head { align-items:center; } .opponent-grade { min-width:64px; height:64px; } }

/* AUTH */
.auth-shell {
  max-width: 520px;
  margin: 70px auto;
  padding: 0 20px;
}
.auth-card {
  background: var(--bg-card);
  border: 1px solid var(--border);
  border-radius: 16px;
  padding: 24px;
  box-shadow: 0 0 35px rgba(0,255,115,0.12);
}
.auth-card h1 {
  font-size: 22px;
  margin-bottom: 6px;
  color: var(--green);
}
.auth-card p {
  color: var(--text-2);
  font-size: 13px;
  margin-bottom: 18px;
}
.auth-field {
  display: grid;
  gap: 6px;
  margin-bottom: 12px;
}
.auth-field label {
  color: var(--text-2);
  font-size: 10px;
  letter-spacing: 1.5px;
  text-transform: uppercase;
  font-weight: 700;
}
.auth-field input {
  background: #000;
  color: var(--text);
  border: 1px solid var(--border-2);
  border-radius: 8px;
  padding: 12px;
  font-family: inherit;
  font-size: 14px;
}
.auth-actions {
  display: flex;
  align-items: center;
  gap: 10px;
  flex-wrap: wrap;
  margin-top: 14px;
}
.auth-link {
  background: transparent;
  border: 0;
  color: var(--green);
  cursor: pointer;
  font-weight: 700;
  letter-spacing: 1px;
}
.auth-error {
  display: none;
  margin-top: 12px;
  padding: 10px 12px;
  border: 1px solid rgba(255,51,68,0.35);
  background: rgba(255,51,68,0.08);
  color: var(--red);
  border-radius: 8px;
  font-size: 13px;
}
.auth-user-box {
  display: flex;
  align-items: center;
  gap: 8px;
  color: var(--text-2);
  font-size: 12px;
}
.auth-user-pill {
  border: 1px solid var(--border-2);
  background: var(--bg-3);
  border-radius: 8px;
  padding: 8px 10px;
  color: var(--text);
}
.auth-role {
  color: var(--green);
  text-transform: uppercase;
  font-weight: 800;
  letter-spacing: 1px;
}
@media (max-width: 768px) {
  .auth-shell { margin: 36px auto; }
  .auth-card { padding: 18px; }
  .auth-user-box { width: 100%; justify-content: flex-end; }
}
/* AGENDA */
.agenda-form {
  background: var(--bg-card);
  border: 1px solid var(--border);
  border-radius: 12px;
  padding: 16px;
  margin-bottom: 16px;
  display: grid;
  grid-template-columns: repeat(6, 1fr);
  gap: 10px;
}
.agenda-form input,
.agenda-form select,
.agenda-form textarea {
  background: var(--bg);
  color: var(--text);
  border: 1px solid var(--border);
  border-radius: 8px;
  padding: 10px;
  font-size: 13px;
  font-family: inherit;
}
.agenda-form textarea { grid-column: span 6; min-height: 60px; resize: vertical; }
.agenda-form .full { grid-column: span 6; display: flex; gap: 8px; justify-content: flex-end; }
.agenda-list {
  display: grid;
  gap: 10px;
}
.agenda-row {
  background: var(--bg-card);
  border: 1px solid var(--border);
  border-radius: 12px;
  padding: 14px 16px;
  display: grid;
  grid-template-columns: 100px 1fr auto auto;
  align-items: center;
  gap: 14px;
}
.agenda-date {
  text-align: center;
  background: rgba(0,255,115,0.1);
  border: 1px solid var(--green-dim);
  border-radius: 8px;
  padding: 6px;
}
.agenda-date .d { font-size: 22px; font-weight: 800; color: var(--green); line-height: 1; }
.agenda-date .m { font-size: 10px; text-transform: uppercase; letter-spacing: 1px; color: var(--text-2); margin-top: 2px; }
.agenda-info .opp { font-size: 16px; font-weight: 700; }
.agenda-info .meta { color: var(--text-2); font-size: 12px; margin-top: 2px; }
.btn-mini {
  background: transparent;
  border: 1px solid var(--border);
  color: var(--text);
  padding: 6px 10px;
  border-radius: 6px;
  font-size: 11px;
  cursor: pointer;
  text-transform: uppercase;
  letter-spacing: 1px;
}
.btn-mini:hover { border-color: var(--green); color: var(--green); }
.btn-mini.danger:hover { border-color: var(--red); color: var(--red); }

.profile-list {
  display: grid;
  gap: 8px;
}
.profile-row {
  background: var(--bg-card);
  border: 1px solid var(--border);
  border-radius: 10px;
  padding: 12px;
  display: grid;
  grid-template-columns: minmax(220px, 1.4fr) repeat(5, minmax(130px, 1fr)) minmax(160px, 1fr) auto;
  gap: 10px;
  align-items: center;
}
.profile-name { font-weight: 800; font-size: 14px; }
.profile-meta { color: var(--text-2); font-size: 11px; margin-top: 3px; line-height: 1.35; }
.profile-row select, .profile-row input {
  background: var(--bg);
  color: var(--text);
  border: 1px solid var(--border);
  border-radius: 8px;
  padding: 9px 10px;
  font-family: inherit;
  font-size: 12px;
}
@media (max-width: 900px) {
  .profile-row { grid-template-columns: 1fr; }
  .profile-row .btn-mini { width: 100%; }
}

/* RESPONSIVE */
@media (max-width: 768px) {
  .stats-grid { grid-template-columns: repeat(2, 1fr); }
  .stat-value { font-size: 28px; }
  .mvp-rating { font-size: 42px; }
  .field { height: 500px; }
  .player-circle { width: 42px; height: 42px; font-size: 12px; }
  .analytics-hero { grid-template-columns: 1fr; }
  .analytics-cards { grid-template-columns: repeat(2, 1fr); }
  .analytics-grid { grid-template-columns: 1fr; }
  .chart-box.wide { grid-column: span 1; }
  .heatmap-wrap { grid-template-columns: 1fr; }
  .heatmap-field { height: 320px; }
}



/* PLAYSTYLE / ARCHETYPE ICONS */
.style-icon {
  width: 48px;
  height: 48px;
  min-width: 48px;
  border-radius: 12px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  color: #07140c;
  background: linear-gradient(135deg, #d9bd66, #fff8dc);
  border: 1px solid rgba(255,255,255,0.35);
  box-shadow: 0 0 16px rgba(0,255,115,0.18);
  font-size: 24px;
  font-weight: 900;
  transform: rotate(45deg);
}
.style-icon span { transform: rotate(-45deg); display:inline-block; }
.style-icon.plus { background: linear-gradient(135deg, #f3f3f3, #ffffff); color:#050505; }
.style-card-head { display:flex; align-items:center; gap:12px; margin-bottom:10px; }
.style-card-title { font-size:18px; font-weight:900; color:var(--text); line-height:1.15; }
.style-card-code { font-size:9px; color:var(--text-3); letter-spacing:1px; text-transform:uppercase; margin-top:3px; }
.style-card-group { color:var(--green); font-size:10px; text-transform:uppercase; letter-spacing:1.6px; margin-bottom:8px; font-weight:800; }
.history-guard-note { color:var(--green); font-size:11px; letter-spacing:1px; margin-top:4px; text-transform:uppercase; }
@media (max-width: 640px) { .style-icon { width:40px; height:40px; min-width:40px; font-size:20px; } }



/* BUILD ATTRIBUTES */
.attr-build-table-wrap { grid-column:1/-1; overflow-x:auto; border:1px solid var(--border); border-radius:12px; background:rgba(255,255,255,0.02); }
.attr-build-table { width:100%; min-width:760px; border-collapse:collapse; font-size:12px; }
.attr-build-table th { text-align:left; color:var(--text-2); font-size:10px; letter-spacing:1.5px; text-transform:uppercase; padding:10px; border-bottom:1px solid var(--border); }
.attr-build-table td { padding:10px; border-bottom:1px solid rgba(255,255,255,0.04); vertical-align:top; }
.attr-build-table tr:last-child td { border-bottom:none; }
.attr-target { color:var(--green); font-size:18px; font-weight:900; }
.attr-priority { display:inline-block; padding:3px 8px; border-radius:999px; background:var(--green-dim); color:var(--green); border:1px solid rgba(0,255,115,.25); font-size:10px; font-weight:900; letter-spacing:1px; }
.attr-phase-grid { grid-column:1/-1; display:grid; grid-template-columns:repeat(auto-fit,minmax(220px,1fr)); gap:10px; }
.attr-phase { background:rgba(255,255,255,0.025); border:1px solid var(--border); border-radius:12px; padding:13px; }
.attr-phase-title { color:var(--green); font-size:12px; font-weight:900; letter-spacing:1.5px; text-transform:uppercase; margin-bottom:8px; }
.attr-phase p { color:var(--text-2); font-size:12px; line-height:1.5; }
.build-explain { grid-column:1/-1; background:rgba(0,255,115,0.04); border:1px solid var(--green-dim); border-radius:12px; padding:14px; color:var(--text-2); font-size:13px; line-height:1.6; }
.build-explain strong { color:var(--green); }
/* BUILD SIMULATOR */
.build-result-grid { display:grid; grid-template-columns:repeat(auto-fit, minmax(280px, 1fr)); gap:12px; width:100%; }
.build-result-grid .section-title { grid-column:1/-1; margin-top:18px; }
.build-card { background:rgba(0,255,115,0.05); border:1px solid var(--border); border-radius:12px; padding:16px; min-height:150px; overflow:hidden; }
.build-card.primary { border-color:rgba(0,255,115,0.45); background:rgba(0,255,115,0.08); }
.build-card .style-card-head { align-items:center; }
.build-card .style-card-title { font-size:16px; line-height:1.2; overflow-wrap:anywhere; }
.build-card .style-card-code { font-size:9px; line-height:1.35; }
.build-card-desc { color:var(--text-2); font-size:12px; margin-top:10px; line-height:1.5; }
.build-why { color:var(--green); font-size:12px; line-height:1.55; margin-top:12px; }
.build-note { color:var(--text-2); font-size:13px; line-height:1.55; }
@media (max-width: 640px) { .build-result-grid { grid-template-columns:1fr; } .build-card { min-height:auto; } }

/* RESPONSIVE HARDENING */
html, body { max-width: 100%; }
.header-inner, .container, .club-card, .period-filter { width: 100%; }
.period-filter { flex-wrap: wrap; align-items: center; }
.tabs { flex-wrap: wrap; border-radius: 14px; scrollbar-width: thin; }
.modal-content, .player-detail { max-width: 100%; overflow-x: hidden; }

@media (max-width: 1024px) {
  .container { padding: 0 12px 32px; }
  .club-card { padding: 0 12px; }
  .stats-grid, .circles-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); }
  .compare-grid, .analytics-grid, .heatmap-wrap { grid-template-columns: 1fr; }
  .chart-box.wide { grid-column: span 1; }
  .compare-bar-row { grid-template-columns: 52px 1fr 72px 1fr 52px; gap: 6px; }
  .modal-box { max-width: calc(100vw - 20px); padding: 18px; }
  .field { width: min(100%, 520px); height: 560px; }
}

@media (max-width: 640px) {
  .header { padding: 12px; }
  .header-inner { align-items: stretch; flex-direction: column; gap: 12px; }
  .header-inner > div:last-child { width: 100%; }
  #clubInput { width: 100% !important; min-width: 0; }
  .btn-sync { justify-content: center; }
  .tabs { margin: 12px; padding: 8px; gap: 6px; }
  .tab { flex: 1 1 calc(50% - 6px); text-align: center; padding: 9px 8px; font-size: 10px; }
  .period-filter { padding: 0 12px; gap: 6px; margin-bottom: 12px; }
  .period { flex: 1 1 calc(50% - 6px); text-align: center; padding: 8px 8px; font-size: 10px; }
  .player-scope-filter .period { flex-basis: 100%; }
  .stats-grid, .circles-grid, .players-grid, .detail-grid, .analytics-cards { grid-template-columns: 1fr 1fr; gap: 8px; }
  .players-grid { grid-template-columns: 1fr; }
  .stat-card { padding: 16px 10px; border-radius: 10px; }
  .stat-value { font-size: 25px; }
  .club-info { padding: 14px; border-radius: 12px; }
  .club-name { font-size: 18px; }
  .match-card, .opp-row, .mvp-card { align-items: flex-start; flex-direction: column; gap: 10px; }
  .opp-stats, .mvp-stats { width: 100%; justify-content: space-between; }
  .field { height: 500px; max-width: 100%; }
  .player-circle { width: 42px; height: 42px; font-size: 12px; }
  .player-circle-name { max-width: 86px; white-space: normal; line-height: 1.15; }
  .agenda-form { grid-template-columns: 1fr !important; }
  .agenda-form input, .agenda-form select, .agenda-form textarea, .agenda-form .full { grid-column: span 1 !important; }
  .agenda-row { grid-template-columns: 72px 1fr; }
  .agenda-row .btn-mini { width: 100%; }
  .history-table { font-size: 10px; min-width: 780px; }
  .player-detail { overflow-x: hidden; }
  .history-table { display: table; }
  .modal-box { max-height: 92vh; border-radius: 10px; }
  .analytics-hero, .heatmap-wrap { grid-template-columns: 1fr; }
  .analytics-score { width: 86px; height: 86px; }
  .chart-box { height: 230px; min-height: 230px; max-height: 230px; }
  .chart-box canvas { height: 180px !important; max-height: 180px !important; }
  .chart-box.wide { height: 300px; min-height: 300px; max-height: 300px; }
  .chart-box.wide canvas { height: 250px !important; max-height: 250px !important; }
}


/* JERSEY FORMATION FIELD */
.field {
  background:
    repeating-linear-gradient(90deg, rgba(255,255,255,0.035) 0 1px, transparent 1px 14.285%),
    repeating-linear-gradient(0deg, rgba(255,255,255,0.025) 0 1px, transparent 1px 11%),
    linear-gradient(180deg, #009d22 0%, #00891d 49%, #007918 50%, #008d1e 100%);
  border: 4px solid rgba(255,255,255,0.9);
  border-radius: 4px;
  height: 720px;
  max-width: 560px;
  box-shadow: 0 0 30px rgba(0,255,115,0.22);
}
.field::before, .field::after {
  border-color: rgba(255,255,255,0.88);
  border-width: 3px;
  width: 52%;
}
.field::before {
  top: 0;
  height: 118px;
  border-radius: 0 0 4px 4px;
}
.field::after {
  bottom: 0;
  height: 118px;
  border-radius: 4px 4px 0 0;
}
.field-line {
  height: 3px;
  background: rgba(255,255,255,0.88);
}
.field-circle {
  width: 124px;
  height: 124px;
  border: 3px solid rgba(255,255,255,0.88);
}
.field-spot {
  position: absolute;
  left: 50%;
  width: 9px;
  height: 9px;
  transform: translateX(-50%);
  border-radius: 50%;
  background: rgba(255,255,255,0.9);
}
.field-spot.top { top: 135px; }
.field-spot.bottom { bottom: 135px; }
.player-on-field {
  width: 112px;
  transform: translate(-50%, -50%);
}
.player-jersey {
  width: 58px;
  height: 62px;
  margin: 0 auto;
  position: relative;
  background: #f6fff8;
  color: #dd1633;
  clip-path: polygon(28% 0, 72% 0, 92% 18%, 100% 39%, 82% 48%, 76% 35%, 76% 100%, 24% 100%, 24% 35%, 18% 48%, 0 39%, 8% 18%);
  filter: drop-shadow(0 3px 4px rgba(0,0,0,0.35));
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 26px;
  font-weight: 900;
  line-height: 1;
}
.player-jersey::before {
  content: '';
  position: absolute;
  top: 0;
  left: 36%;
  width: 28%;
  height: 10px;
  border: 3px solid #dd1633;
  border-top: none;
  border-radius: 0 0 12px 12px;
}
.player-jersey.gk {
  background: #ffd233;
  color: #111;
}
.player-jersey.gk::before { border-color: #111; }
.player-circle { display: none; }
.player-circle-name {
  display: block;
  min-width: 92px;
  max-width: 112px;
  margin: 3px auto 0;
  padding: 2px 8px;
  background: rgba(0, 42, 20, 0.86);
  border-radius: 999px;
  color: #fff;
  font-size: 9px;
  font-weight: 800;
  line-height: 1.15;
  text-align: center;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
  box-shadow: 0 2px 8px rgba(0,0,0,0.24);
}
.player-role-label {
  color: rgba(255,255,255,0.86);
  font-size: 8px;
  font-weight: 900;
  letter-spacing: 1px;
  text-align: center;
  margin-top: 1px;
}
.player-rating-label {
  color: #eaffef;
  font-size: 10px;
  font-weight: 900;
  text-align: center;
  text-shadow: 0 1px 5px rgba(0,0,0,0.55);
  margin-top: 1px;
}
@media (max-width: 640px) {
  .field { height: 620px; }
  .player-on-field { width: 92px; }
  .player-jersey { width: 46px; height: 50px; font-size: 21px; }
  .player-circle-name { min-width: 76px; max-width: 92px; font-size: 8px; padding: 2px 6px; }
  .player-rating-label { font-size: 9px; }
}


/* FORMATION VISUAL POLISH */
.formation-wrapper {
  overflow: visible;
}
.field {
  height: 780px;
  max-width: 640px;
  aspect-ratio: 7 / 10;
}
.player-on-field {
  width: 126px;
  z-index: 3;
}
.player-jersey {
  width: 54px;
  height: 58px;
  font-size: 24px;
}
.player-circle-name {
  min-width: 104px;
  max-width: 124px;
  font-size: 9px;
  padding: 3px 8px;
}
.player-rating-label {
  font-size: 10px;
  line-height: 1.05;
}
.player-role-label {
  font-size: 8px;
  line-height: 1.05;
}
@media (max-width: 760px) {
  .field {
    height: 680px;
    max-width: 100%;
  }
  .player-on-field { width: 94px; }
  .player-jersey { width: 42px; height: 46px; font-size: 19px; }
  .player-circle-name { min-width: 78px; max-width: 94px; font-size: 8px; padding: 2px 5px; }
  .player-rating-label { font-size: 8px; }
  .player-role-label { font-size: 7px; }
}

</style>
</head>
<body>

<div class="header">
  <div class="header-inner">
    <div class="logo">
      <div class="logo-icon">⚽</div>
      <div>
        <div class="logo-text">SCOUT <span>CLUBS</span></div>
        <div class="logo-sub">Inteligência Esportiva</div>
      </div>
    </div>
    <div style="display:flex;gap:8px;align-items:center;">
      <input id="clubInput" type="text" placeholder="Nome do clube" value=""
        style="background:var(--bg-3);border:1px solid var(--border-2);color:var(--text);padding:8px 12px;border-radius:8px;font-size:13px;outline:none;width:180px;font-family:inherit;"
        onkeydown="if(event.key==='Enter') startSync()">
      <button id="syncButton" class="btn-sync" onclick="startSync()">↻ Sincronizar</button>
      <div id="authBox" class="auth-user-box"></div>
    </div>
  </div>
</div>

<div id="syncProgress" class="sync-progress">
  <div class="sync-title">🔄 Sincronizando dados do clube...</div>
  <div class="sync-step" id="syncStep">Iniciando...</div>
  <div class="sync-progress-bar">
    <div class="sync-progress-fill" id="syncFill"></div>
  </div>
  <div class="sync-log" id="syncLog"></div>
</div>

<div id="content"></div>

<div class="modal-bg" id="modal" onclick="if(event.target===this) closeModal()">
  <div class="modal-box">
    <button class="modal-close" onclick="closeModal()">×</button>
    <div class="modal-content" id="modalContent"></div>
  </div>
</div>

<script>
let DATA = null;
let CURRENT_TAB = 'visao';
let CURRENT_PERIOD = 'todos';
let CURRENT_MATCH_TYPE = 'todos';
let PLAYER_PROFILES = {};
let COMPARE_A = null;
let COMPARE_B = null;
let AGENDA = [];
let OPPONENT_SCOUTS = [];
let AGENDA_EDIT_ID = null;
let IDEAL_FORMATION = '3-5-2';
let AUTH_TOKEN = localStorage.getItem('scout_auth_token') || '';
let AUTH_USER = (() => {
  try { return JSON.parse(localStorage.getItem('scout_auth_user') || 'null'); }
  catch (e) { return null; }
})();

function isLoggedIn() { return !!AUTH_TOKEN && !!AUTH_USER; }
function isAdmin() { return !!AUTH_USER && AUTH_USER.cargo === 'admin'; }

function authHeaders(extra = {}) {
  const headers = {...extra};
  if (AUTH_TOKEN) headers['Authorization'] = 'Bearer ' + AUTH_TOKEN;
  return headers;
}

async function authFetch(url, opts = {}) {
  const options = {...opts};
  options.headers = authHeaders(options.headers || {});
  const r = await fetch(url, options);
  if (r.status === 401 || r.status === 403) {
    if (r.status === 401) logout(false);
  }
  return r;
}

function updateAuthHeader() {
  const box = document.getElementById('authBox');
  const syncBtn = document.getElementById('syncButton');
  const clubInput = document.getElementById('clubInput');
  if (syncBtn) syncBtn.style.display = isAdmin() ? 'flex' : 'none';
  if (clubInput) clubInput.style.display = isLoggedIn() ? 'block' : 'none';
  if (!box) return;
  if (!isLoggedIn()) {
    box.innerHTML = '';
    return;
  }
  box.innerHTML = `<div class="auth-user-pill">${AUTH_USER.nome || AUTH_USER.usuario} · <span class="auth-role">${AUTH_USER.cargo}</span></div><button class="btn-mini" onclick="logout()">Sair</button>`;
}

function showAuthError(msg) {
  const el = document.getElementById('authError');
  if (!el) return;
  el.textContent = msg || 'Erro de autenticação';
  el.style.display = 'block';
}

function renderAuth(mode = 'login') {
  updateAuthHeader();
  const c = document.getElementById('content');
  if (!c) return;
  const isRegister = mode === 'register';
  if (!isAdmin() && ['jogadores','comparar','confrontos','adversarios'].includes(CURRENT_TAB)) CURRENT_TAB = 'visao';
  c.innerHTML = `
    <div class="auth-shell">
      <div class="auth-card">
        <h1>${isRegister ? 'Criar Conta' : 'Entrar no Scout Clubs'}</h1>
        <p>${isRegister ? 'Crie sua conta como jogador. O acesso ao sistema fica liberado quando o clube tiver um admin ativo.' : 'Use seu usuário e senha para acessar o scout do seu clube.'}</p>
        <form onsubmit="${isRegister ? 'submitRegister(event)' : 'submitLogin(event)'}">
          ${isRegister ? `<div class="auth-field"><label>Nome</label><input id="authNome" autocomplete="name" required></div>` : ''}
          <div class="auth-field"><label>Usuário</label><input id="authUsuario" autocomplete="username" required></div>
          <div class="auth-field"><label>Senha</label><input id="authSenha" type="password" autocomplete="${isRegister ? 'new-password' : 'current-password'}" required></div>
          ${isRegister ? `
            <div class="auth-field"><label>Nome do Clube</label><input id="authClube" placeholder="Nome do clube" required></div>
          ` : ''}
          <div class="auth-actions">
            <button class="btn-primary" type="submit">${isRegister ? 'Criar Conta' : 'Entrar'}</button>
            <button class="auth-link" type="button" onclick="renderAuth('${isRegister ? 'login' : 'register'}')">${isRegister ? 'Já tenho conta' : 'Criar conta'}</button>
          </div>
          <div id="authError" class="auth-error"></div>
        </form>
      </div>
    </div>`;
}

async function submitLogin(ev) {
  ev.preventDefault();
  try {
    const body = {
      usuario: document.getElementById('authUsuario').value.trim(),
      senha: document.getElementById('authSenha').value,
    };
    const r = await fetch('/api/auth/login', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify(body)});
    const data = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(data.detail || data.message || 'Login inválido');
    AUTH_TOKEN = data.access_token;
    AUTH_USER = data.user;
    localStorage.setItem('scout_auth_token', AUTH_TOKEN);
    localStorage.setItem('scout_auth_user', JSON.stringify(AUTH_USER));
    updateAuthHeader();
    await loadData();
  } catch (e) {
    showAuthError(e.message);
  }
}

async function submitRegister(ev) {
  ev.preventDefault();
  try {
    const body = {
      nome: document.getElementById('authNome').value.trim(),
      usuario: document.getElementById('authUsuario').value.trim(),
      senha: document.getElementById('authSenha').value,
      clube: document.getElementById('authClube').value.trim(),
    };
    const r = await fetch('/api/auth/register', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify(body)});
    const data = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(data.detail || data.message || 'Erro ao criar conta');
    renderAuth('login');
    setTimeout(() => showAuthError('Conta criada como jogador. Se ainda não houver admin ativo, o login ficará bloqueado até a liberação.'), 50);
  } catch (e) {
    showAuthError(e.message);
  }
}

function logout(render = true) {
  AUTH_TOKEN = '';
  AUTH_USER = null;
  DATA = null;
  localStorage.removeItem('scout_auth_token');
  localStorage.removeItem('scout_auth_user');
  updateAuthHeader();
  if (render) renderAuth('login');
}

async function initAuth() {
  updateAuthHeader();
  if (!AUTH_TOKEN) {
    renderAuth('login');
    return;
  }
  try {
    const r = await authFetch('/api/auth/me');
    const data = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(data.detail || 'Sessão expirada');
    AUTH_USER = data.user;
    localStorage.setItem('scout_auth_user', JSON.stringify(AUTH_USER));
    updateAuthHeader();
    await loadData();
  } catch (e) {
    logout(false);
    renderAuth('login');
  }
}

const PLAYSTYLE_CATALOG = [
  {name:'Chute Forte', code:'Power Shot', group:'Finalização', desc:'Chutes fortes de média/longa distância com mais potência.'},
  {name:'Bola Parada', code:'Dead Ball', group:'Finalização', desc:'Faltas, escanteios e bolas paradas com mais curva e precisão.'},
  {name:'Cavadinha', code:'Chip Shot', group:'Finalização', desc:'Cavadinhas e finalizações por cobertura mais eficientes.'},
  {name:'Chute Colocado', code:'Finesse Shot', group:'Finalização', desc:'Chutes colocados com curva e precisão.'},
  {name:'Cabeceio Forte', code:'Power Header', group:'Finalização', desc:'Cabeceios ofensivos mais fortes e precisos.'},
  {name:'Passe Incisivo', code:'Incisive Pass', group:'Passe', desc:'Enfiadas e passes que quebram linhas.'},
  {name:'Passe Pingado', code:'Pinged Pass', group:'Passe', desc:'Passes rasteiros fortes com velocidade e controle.'},
  {name:'Lançamento Longo', code:'Long Ball Pass', group:'Passe', desc:'Lançamentos longos mais precisos.'},
  {name:'Tiki-Taka', code:'Tiki Taka', group:'Passe', desc:'Passes curtos de primeira e combinações rápidas.'},
  {name:'Cruzamento Tenso', code:'Whipped Pass', group:'Passe', desc:'Cruzamentos com curva, velocidade e perigo.'},
  {name:'Primeiro Toque', code:'First Touch', group:'Controle', desc:'Primeiro toque orientado e domínio sob pressão.'},
  {name:'Estilo', code:'Flair', group:'Controle', desc:'Passes e finalizações plásticas com mais eficácia.'},
  {name:'Resistente à Pressão', code:'Press Proven', group:'Controle', desc:'Protege a bola melhor sob pressão.'},
  {name:'Rápido com Bola', code:'Rapid', group:'Controle', desc:'Corridas em velocidade com a bola.'},
  {name:'Técnico', code:'Technical', group:'Controle', desc:'Condução técnica e dribles controlados.'},
  {name:'Driblador', code:'Trickster', group:'Controle', desc:'Dribles especiais e movimentos de habilidade.'},
  {name:'Bloqueio', code:'Block', group:'Defesa', desc:'Bloqueios defensivos mais eficazes.'},
  {name:'Brigador', code:'Bruiser', group:'Defesa', desc:'Duelos físicos e disputas de corpo mais fortes.'},
  {name:'Interceptação', code:'Intercept', group:'Defesa', desc:'Interceptações e cortes de passe melhores.'},
  {name:'Contenção', code:'Jockey', group:'Defesa', desc:'Contenção lateral e marcação em jockey mais eficiente.'},
  {name:'Carrinho', code:'Slide Tackle', group:'Defesa', desc:'Carrinhos com maior alcance e precisão.'},
  {name:'Antecipação', code:'Anticipate', group:'Defesa', desc:'Botes em pé e antecipações mais limpos.'},
  {name:'Acrobático', code:'Acrobatic', group:'Físico', desc:'Voleios, bicicletas e ações acrobáticas.'},
  {name:'Jogo Aéreo', code:'Aerial', group:'Físico', desc:'Disputas aéreas ofensivas e defensivas.'},
  {name:'Trivela', code:'Trivela', group:'Físico', desc:'Passes e chutes de três dedos.'},
  {name:'Incansável', code:'Relentless', group:'Físico', desc:'Fôlego, recomposição e pressão por mais tempo.'},
  {name:'Arranque', code:'Quick Step', group:'Físico', desc:'Explosão nos primeiros metros.'},
  {name:'Arremesso Longo', code:'Long Throw', group:'Físico', desc:'Laterais longos para área ou profundidade.'},
  {name:'Reposição Longa', code:'Far Throw', group:'Goleiro', desc:'Reposição longa com as mãos.'},
  {name:'Defesa com os Pés', code:'Footwork', group:'Goleiro', desc:'Defesas com os pés e ajustes curtos.'},
  {name:'Domínio da Área', code:'Cross Claimer', group:'Goleiro', desc:'Saídas em cruzamentos.'},
  {name:'Saída Rápida', code:'Rush Out', group:'Goleiro', desc:'Saídas rápidas do gol para abafar.'},
  {name:'Alcance Longo', code:'Far Reach', group:'Goleiro', desc:'Alcance em defesas no canto.'},
  {name:'Reflexos Rápidos', code:'Quick Reflexes', group:'Goleiro', desc:'Reflexos em chutes próximos.'},
];

const ARCHETYPE_CATALOG = [
  {name:'Chefia', group:'Defesa', desc:'Perfil de liderança defensiva: organiza a linha, ganha duelos e protege a área.'},
  {name:'Líbero', group:'Defesa', desc:'Zagueiro que antecipa, cobre profundidade e ajuda na saída de bola.'},
  {name:'Cão de Guarda', group:'Defesa', desc:'Marcador agressivo para pressão, bote e combate no meio.'},
  {name:'Progressor', group:'Defesa', desc:'Zagueiro moderno que avança com segurança e inicia ataques com passe progressivo.'},
  {name:'Motor', group:'Meio-campo', desc:'Meio-campista de ida e volta, pressão constante e apoio nas duas fases.'},
  {name:'Maestro', group:'Meio-campo', desc:'Criador que controla ritmo, acha passes e organiza a construção.'},
  {name:'Camisa 10', group:'Meio-campo', desc:'Meia ofensivo de criação, último passe e tomada de decisão perto da área.'},
  {name:'Ala Criador', group:'Meio-campo', desc:'Jogador aberto para amplitude, cruzamentos e apoio ofensivo.'},
  {name:'Ponta Agudo', group:'Ataque', desc:'Atacante de lado com velocidade, drible e ataque ao espaço.'},
  {name:'Matador', group:'Ataque', desc:'Finalizador central, foco em gols, posicionamento e decisão na área.'},
  {name:'Falso 9', group:'Ataque', desc:'Atacante que sai da área, conecta jogadas e cria para quem infiltra.'},
  {name:'Referência', group:'Ataque', desc:'Centroavante físico para pivô, proteção e jogo aéreo.'},
  {name:'Paredão', group:'Goleiro', desc:'Goleiro de reflexo e defesa de chutes próximos.'},
  {name:'Goleiro Líbero', group:'Goleiro', desc:'Goleiro que sai do gol, cobre profundidade e inicia jogadas.'},
];

function filteredMatches() {
  let all = (DATA && DATA.matches) ? [...DATA.matches] : [];
  if (CURRENT_MATCH_TYPE !== 'todos') {
    all = all.filter(m => String(m.match_type || '').toLowerCase() === CURRENT_MATCH_TYPE);
  }
  // Já vem ordenado por timestamp desc
  if (CURRENT_PERIOD === 'todos') return all;
  if (CURRENT_PERIOD === 'ult5') return all.slice(0, 5);
  if (CURRENT_PERIOD === 'ult10') return all.slice(0, 10);
  const now = Math.floor(Date.now() / 1000);
  if (CURRENT_PERIOD === 'semana') {
    const cutoff = now - 7 * 86400;
    return all.filter(m => (m.timestamp || 0) >= cutoff);
  }
  if (CURRENT_PERIOD === 'mes') {
    const cutoff = now - 30 * 86400;
    return all.filter(m => (m.timestamp || 0) >= cutoff);
  }
  return all;
}

function playerStatMatches() {
  let all = (DATA && DATA.matches) ? [...DATA.matches] : [];
  if (CURRENT_MATCH_TYPE !== 'todos') {
    all = all.filter(m => String(m.match_type || '').toLowerCase() === CURRENT_MATCH_TYPE);
  }
  return all;
}
function setPeriod(p, ev) {
  CURRENT_PERIOD = p;
  document.querySelectorAll('.period').forEach(el => el.classList.remove('active'));
  if (ev && ev.target) ev.target.classList.add('active');
  if (!isAdmin()) {
    document.querySelectorAll('.tab').forEach(el => {
      if (['JOGADORES','COMPARAR','CONFRONTOS','ADVERSÁRIOS'].includes((el.textContent || '').trim())) el.remove();
    });
  }
  renderTab();
}


function setMatchType(t, ev) {
  CURRENT_MATCH_TYPE = t;
  document.querySelectorAll('.matchtype').forEach(el => el.classList.remove('active'));
  if (ev && ev.target) ev.target.classList.add('active');
  if (!isAdmin()) {
    document.querySelectorAll('.tab').forEach(el => {
      if (['JOGADORES','COMPARAR','CONFRONTOS','ADVERSÁRIOS'].includes((el.textContent || '').trim())) el.remove();
    });
  }
  renderTab();
}

function computePlayersForMatches(matches) {
  const byName = {};
  (DATA.players || []).forEach(p => {
    byName[p.name] = {
      ...p,
      favorite_position: p.position || '?',
      last_match_position: '',
      position_counts: {GK:0, DEF:0, MID:0, FWD:0},
      games: 0, rating_sum: 0, sofi_sum: 0, goals: 0, assists: 0, shots: 0,
      passes_pct_sum: 0, passes_made: 0, tackle_pct_sum: 0, tackles_made: 0, mom: 0, reds: 0, saves: 0, clean_sheet: 0, wins: 0, draws: 0, losses: 0
    };
  });
  (matches || []).forEach(m => {
    (m.players_ratings || []).forEach(pr => {
      if (!byName[pr.name]) {
        byName[pr.name] = {
          name: pr.name, position: pr.pos || '?', favorite_position: pr.pos || '?', last_match_position: '',
          position_counts: {GK:0, DEF:0, MID:0, FWD:0},
          games: 0, rating_sum: 0, sofi_sum: 0, goals: 0, assists: 0, shots: 0,
          passes_pct_sum: 0, passes_made: 0, tackle_pct_sum: 0, tackles_made: 0, mom: 0, reds: 0, saves: 0, clean_sheet: 0, wins: 0, draws: 0, losses: 0
        };
      }
      const p = byName[pr.name];
      const fam = normalizePlayerFamily(pr.pos);
      p.games += 1;
      p.last_match_position = pr.pos || p.last_match_position;
      p.position_counts[fam] = (p.position_counts[fam] || 0) + 1;
      p.rating_sum += Number(pr.rating || 0);
      p.sofi_sum += Number(pr.sofi_rating || pr.rating || 0);
      p.goals += Number(pr.goals || 0);
      p.assists += Number(pr.assists || 0);
      p.shots += Number(pr.shots || 0);
      p.passes_pct_sum += Number(pr.pass_pct || 0);
      p.passes_made += Number(pr.passes_made || 0);
      p.tackle_pct_sum += Number(pr.tackle_pct || 0);
      p.tackles_made += Number(pr.tackles_made || 0);
      p.mom += Number(pr.mom || 0);
      if (m.result === 'V') p.wins += 1; else if (m.result === 'E') p.draws += 1; else if (m.result === 'D') p.losses += 1;
      p.reds += Number(pr.red || 0);
      p.saves += Number(pr.saves || 0);
      p.clean_sheet += Number(pr.clean_sheet || 0);
    });
  });
  return Object.values(byName)
    .filter(p => p.games > 0)
    .map(p => {
      const intel = inferPlayerPositionIntel(p);
      return {
        ...p,
        position: intel.label,
        position_family: intel.family,
        position_source: intel.source,
        rating: +(p.rating_sum / Math.max(p.games, 1)).toFixed(2),
        sofi_rating: +(p.sofi_sum / Math.max(p.games, 1)).toFixed(2),
        pass_pct: +(p.passes_pct_sum / Math.max(p.games, 1)).toFixed(1),
        tackle_pct: +(p.tackle_pct_sum / Math.max(p.games, 1)).toFixed(1),
        goals_per_game: +(p.goals / Math.max(p.games, 1)).toFixed(2),
        assists_per_game: +(p.assists / Math.max(p.games, 1)).toFixed(2),
        shots_per_game: +(p.shots / Math.max(p.games, 1)).toFixed(2),
        tackles_per_game: +(p.tackles_made / Math.max(p.games, 1)).toFixed(2),
        saves_per_game: +(p.saves / Math.max(p.games, 1)).toFixed(2),
        goal_involvements: Number(p.goals || 0) + Number(p.assists || 0),
        goal_involvements_per_game: +((Number(p.goals || 0) + Number(p.assists || 0)) / Math.max(p.games, 1)).toFixed(2),
        win_rate: +((Number(p.wins || 0) / Math.max(p.games, 1)) * 100).toFixed(1),
      };
    })
    .sort((a,b) => Number(b.rating || 0) - Number(a.rating || 0));
}

function scopedPlayers() {
  return computePlayersForMatches(playerStatMatches());
}




function clubHistoryKey(club) {
  const id = club && club.id ? String(club.id) : 'default';
  const name = club && club.name ? String(club.name).toLowerCase().replace(/[^a-z0-9]+/g, '_') : 'club';
  return 'scout_match_history_' + id + '_' + name;
}

function validMatchIdValue(value) {
  const s = String(value || '').trim().toLowerCase();
  return s && !['none','null','undefined','0'].includes(s);
}

function stableClientMatchId(match, club) {
  if (validMatchIdValue(match?.match_id)) return String(match.match_id).trim();
  if (validMatchIdValue(match?.matchId)) return String(match.matchId).trim();
  const clubId = club && club.id ? club.id : 'club';
  return [clubId, match?.timestamp || '', match?.opponent || '', match?.score || '', match?.match_type || ''].join(':');
}

function mergeMatchHistoryForClub(club, ...groups) {
  const merged = new Map();
  groups.forEach(group => {
    (group || []).forEach(match => {
      if (!match || typeof match !== 'object') return;
      const key = stableClientMatchId(match, club);
      const old = merged.get(key) || {};
      merged.set(key, {...old, ...match, match_id: key});
    });
  });
  return [...merged.values()].sort((a,b) => Number(b.timestamp || 0) - Number(a.timestamp || 0));
}

function loadLocalMatchHistory(club) {
  try {
    return JSON.parse(localStorage.getItem(clubHistoryKey(club)) || '[]') || [];
  } catch (e) {
    console.warn('Historico local invalido', e);
    return [];
  }
}

function saveLocalMatchHistory(club, matches) {
  try {
    if (!club || !club.id) return;
    const merged = mergeMatchHistoryForClub(club, matches || []);
    localStorage.setItem(clubHistoryKey(club), JSON.stringify(merged));
  } catch (e) {
    console.warn('Nao salvou historico local', e);
  }
}

function protectDataMatchHistory() {
  if (!DATA || !DATA.club) return;
  const serverMatches = DATA.matches || [];
  const localMatches = loadLocalMatchHistory(DATA.club);
  const merged = mergeMatchHistoryForClub(DATA.club, localMatches, serverMatches);
  if (merged.length < localMatches.length) {
    DATA.matches = localMatches;
  } else {
    DATA.matches = merged;
  }
  saveLocalMatchHistory(DATA.club, DATA.matches);
}

function profileStorageKey() {
  const clubId = DATA && DATA.club ? DATA.club.id : 'default';
  return 'scout_player_profiles_' + clubId;
}

function loadLocalPlayerProfiles() {
  try {
    return JSON.parse(localStorage.getItem(profileStorageKey()) || '{}') || {};
  } catch (e) {
    return {};
  }
}

function saveLocalPlayerProfiles() {
  try {
    localStorage.setItem(profileStorageKey(), JSON.stringify(PLAYER_PROFILES || {}));
  } catch (e) {
    console.warn('Nao salvou perfis no navegador', e);
  }
}

async function loadPlayerProfiles() {
  const localProfiles = loadLocalPlayerProfiles();
  const dashboardProfiles = (DATA && DATA.player_profiles) ? DATA.player_profiles : {};
  try {
    const r = await authFetch('/api/player-profiles');
    const data = await r.json();
    // Ordem importa: o ajuste manual salvo no navegador e na sessão atual vence o cache/API da EA após sincronizar.
    PLAYER_PROFILES = {...dashboardProfiles, ...(data.profiles || {}), ...localProfiles, ...(PLAYER_PROFILES || {})};
  } catch (e) {
    PLAYER_PROFILES = {...dashboardProfiles, ...localProfiles, ...(PLAYER_PROFILES || {})};
    console.warn('Perfis manuais via API indisponiveis; usando cache local/dashboard', e);
  }
  saveLocalPlayerProfiles();
}

async function savePlayerProfile(name, manualPosition, archetype, notes, playstyles=[]) {
  playstyles = (playstyles || []).filter(Boolean).slice(0, 3);
  if (manualPosition || archetype || notes || playstyles.length) {
    PLAYER_PROFILES[name] = {manual_position: manualPosition || null, archetype: archetype || null, playstyles, notes: notes || null, manual_saved_at: new Date().toISOString()};
    if (DATA) DATA.player_profiles = {...(DATA.player_profiles || {}), [name]: PLAYER_PROFILES[name]};
  } else {
    delete PLAYER_PROFILES[name];
    if (DATA && DATA.player_profiles) delete DATA.player_profiles[name];
  }
  saveLocalPlayerProfiles();
  const r = await authFetch('/api/player-profiles/' + encodeURIComponent(name), {
    method: 'PUT',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({
      manual_position: manualPosition || null,
      archetype: archetype || null,
      playstyles,
      notes: notes || null,
    })
  });
  if (!r.ok) throw new Error('Erro ao salvar cadastro');
  await loadPlayerProfiles();
}

async function loadData() {
  if (!isLoggedIn()) { renderAuth('login'); return; }
  updateAuthHeader();
  try {
    const r = await authFetch('/api/dashboard');
    if (!r.ok) throw new Error('Falha ao carregar dashboard');
    DATA = await r.json();
    protectDataMatchHistory();
    await loadPlayerProfiles();
    await loadAgenda();
    render();
  } catch (e) {
    console.error(e);
  }
}

function render() {
  const c = document.getElementById('content');
  
  if (!DATA || !DATA.club) {
    if (!isAdmin() && ['jogadores','comparar','confrontos','adversarios'].includes(CURRENT_TAB)) CURRENT_TAB = 'visao';
  c.innerHTML = `
      <div class="empty-state">
        <div class="empty-icon">⚽</div>
        <div class="empty-title">Nenhum clube sincronizado</div>
        <div class="empty-text">Clique no botão abaixo para sincronizar seu clube EA FC</div>
        <button class="btn-primary" onclick="startSync()">↻ SINCRONIZAR CLUBE</button>
      </div>`;
    return;
  }
  
  if (!isAdmin() && ['jogadores','comparar','confrontos','adversarios'].includes(CURRENT_TAB)) CURRENT_TAB = 'visao';
  c.innerHTML = `
    <div class="club-card">
      <div class="club-info">
        <div class="club-shield">🛡️</div>
        <div>
          <div class="club-name">${DATA.club.name}</div>
          <div class="club-meta">${computePlayersForMatches(DATA.matches || []).length} jogadores com partidas no clube · ${DATA.matches?.length || 0} partidas</div>
        </div>
      </div>
    </div>
    
    <div class="tabs">
      <div class="tab ${CURRENT_TAB==='visao'?'active':''}" onclick="setTab('visao')">VISÃO</div>
      <div class="tab ${CURRENT_TAB==='jogadores'?'active':''}" onclick="setTab('jogadores')">JOGADORES</div>
      <div class="tab ${CURRENT_TAB==='comparar'?'active':''}" onclick="setTab('comparar')">COMPARAR</div>
      <div class="tab ${CURRENT_TAB==='confrontos'?'active':''}" onclick="setTab('confrontos')">CONFRONTOS</div>
      <div class="tab ${CURRENT_TAB==='time-ideal'?'active':''}" onclick="setTab('time-ideal')">TIME IDEAL</div>
      <div class="tab ${CURRENT_TAB==='cadastro'?'active':''}" onclick="setTab('cadastro')">CADASTRO</div>
      <div class="tab ${CURRENT_TAB==='playstyles'?'active':''}" onclick="setTab('playstyles')">ESTILOS</div>
      <div class="tab ${CURRENT_TAB==='adversarios'?'active':''}" onclick="setTab('adversarios')">ADVERSÁRIOS</div>
      <div class="tab ${CURRENT_TAB==='agenda'?'active':''}" onclick="setTab('agenda')">AGENDA</div>
    </div>
    
    <div class="period-filter">
      <div class="period ${CURRENT_PERIOD==='todos'?'active':''}" onclick="setPeriod('todos', event)">TODOS</div>
      <div class="period ${CURRENT_PERIOD==='ult5'?'active':''}" onclick="setPeriod('ult5', event)">ÚLT. 5</div>
      <div class="period ${CURRENT_PERIOD==='ult10'?'active':''}" onclick="setPeriod('ult10', event)">ÚLT. 10</div>
      <div class="period ${CURRENT_PERIOD==='semana'?'active':''}" onclick="setPeriod('semana', event)">SEMANA</div>
      <div class="period ${CURRENT_PERIOD==='mes'?'active':''}" onclick="setPeriod('mes', event)">MÊS</div>
    </div>
    
    <div class="period-filter" style="margin-top:-10px;">
      <div class="period matchtype ${CURRENT_MATCH_TYPE==='todos'?'active':''}" onclick="setMatchType('todos', event)">TODAS PARTIDAS</div>
      <div class="period matchtype ${CURRENT_MATCH_TYPE==='liga'?'active':''}" onclick="setMatchType('liga', event)">LIGA</div>
      <div class="period matchtype ${CURRENT_MATCH_TYPE==='copa'?'active':''}" onclick="setMatchType('copa', event)">COPA</div>
      <div class="period matchtype ${CURRENT_MATCH_TYPE==='amistoso'?'active':''}" onclick="setMatchType('amistoso', event)">AMISTOSO</div>
    </div>

    
    <div class="container">
      <div id="tabContent"></div>
    </div>
  `;
  
  if (!isAdmin()) {
    document.querySelectorAll('.tab').forEach(el => {
      if (['JOGADORES','COMPARAR','CONFRONTOS','ADVERSÁRIOS'].includes((el.textContent || '').trim())) el.remove();
    });
  }
  renderTab();
}

function setTab(t) {
  CURRENT_TAB = t;
  document.querySelectorAll('.tab').forEach(el => el.classList.remove('active'));
  event.target.classList.add('active');
  if (!isAdmin()) {
    document.querySelectorAll('.tab').forEach(el => {
      if (['JOGADORES','COMPARAR','CONFRONTOS','ADVERSÁRIOS'].includes((el.textContent || '').trim())) el.remove();
    });
  }
  renderTab();
}

function renderTab() {
  const tc = document.getElementById('tabContent');
  if (!tc) return;
  
  if (CURRENT_TAB === 'visao') tc.innerHTML = renderVisao();
  else if (CURRENT_TAB === 'jogadores') tc.innerHTML = renderJogadores();
  else if (CURRENT_TAB === 'comparar') { tc.innerHTML = renderComparar(); renderCompareBars(); }
  else if (CURRENT_TAB === 'confrontos') tc.innerHTML = renderConfrontos();
  else if (CURRENT_TAB === 'time-ideal') tc.innerHTML = renderTimeIdeal();
  else if (CURRENT_TAB === 'cadastro') tc.innerHTML = renderCadastroJogadores();
  else if (CURRENT_TAB === 'playstyles') tc.innerHTML = renderPlaystyles();
  else if (CURRENT_TAB === 'adversarios') tc.innerHTML = renderAdversarios();
  else if (['jogadores','comparar','confrontos','adversarios'].includes(CURRENT_TAB) && !isAdmin()) { CURRENT_TAB = 'visao'; tc.innerHTML = renderVisao(); }
  else if (CURRENT_TAB === 'agenda') tc.innerHTML = renderAgenda();
}

function computeStatsFor(matches) {
  const out = {wins:0, draws:0, losses:0, goals_for:0, goals_against:0, clean_sheets:0, matches_played: matches.length, best_streak: 0};
  let streak = 0;
  matches.slice().reverse().forEach(m => {
    out.goals_for += m.goals_for || 0;
    out.goals_against += m.goals_against || 0;
    if (m.goals_against === 0) out.clean_sheets++;
    if (m.result === 'V') { out.wins++; streak++; if (streak > out.best_streak) out.best_streak = streak; }
    else if (m.result === 'E') { out.draws++; streak = 0; }
    else { out.losses++; streak = 0; }
  });
  out.goal_diff = out.goals_for - out.goals_against;
  out.win_rate = matches.length ? Math.round((out.wins / matches.length) * 100) : 0;
  out.goals_per_match = matches.length ? +(out.goals_for / matches.length).toFixed(2) : 0;
  return out;
}

function calcOpponentAvgClient(matches) {
  const byOpp = {};
  (matches || []).forEach(m => {
    const opp = m.opponent || 'Adversário';
    if (!byOpp[opp]) byOpp[opp] = {games:0, gf:0, ga:0};
    byOpp[opp].games += 1;
    byOpp[opp].gf += Number(m.goals_for || 0);
    byOpp[opp].ga += Number(m.goals_against || 0);
  });
  return Object.entries(byOpp).map(([opponent, d]) => ({
    opponent,
    games: d.games,
    avg_gf: +(d.gf / Math.max(d.games, 1)).toFixed(1),
    avg_ga: +(d.ga / Math.max(d.games, 1)).toFixed(1),
  })).sort((a,b) => b.avg_gf - a.avg_gf).slice(0, 10);
}

function avgFrom(values) {
  const nums = (values || []).map(Number).filter(v => Number.isFinite(v));
  return nums.length ? nums.reduce((a,b) => a + b, 0) / nums.length : 0;
}

function computeAdvancedGeneralStats(matches) {
  const out = {
    player_apps: 0,
    avg_players: 0,
    total_shots: 0,
    shots_per_match: 0,
    avg_pass_pct: 0,
    avg_tackle_pct: 0,
    tackles_made: 0,
    saves: 0,
    red_cards: 0,
    moms: 0,
    games_3gf: 0,
    games_3ga: 0,
    comeback_wins: 0,
    best_attack: 0,
    worst_defense: 0,
    best_diff: 0,
    worst_diff: 0,
    scoreless_games: 0,
    conceded_games: 0,
    avg_sofi: 0,
    avg_ea: 0,
  };
  const passVals = [];
  const tackleVals = [];
  const sofiVals = [];
  const eaVals = [];
  (matches || []).forEach(m => {
    const gf = Number(m.goals_for || 0);
    const ga = Number(m.goals_against || 0);
    out.best_attack = Math.max(out.best_attack, gf);
    out.worst_defense = Math.max(out.worst_defense, ga);
    out.best_diff = Math.max(out.best_diff, gf - ga);
    out.worst_diff = Math.min(out.worst_diff, gf - ga);
    if (gf >= 3) out.games_3gf += 1;
    if (ga >= 3) out.games_3ga += 1;
    if (gf === 0) out.scoreless_games += 1;
    if (ga > 0) out.conceded_games += 1;
    const prs = m.players_ratings || [];
    out.player_apps += prs.length;
    prs.forEach(p => {
      out.total_shots += Number(p.shots || 0);
      out.tackles_made += Number(p.tackles_made || 0);
      out.saves += Number(p.saves || 0);
      out.red_cards += Number(p.red || 0);
      out.moms += Number(p.mom || 0);
      if (Number(p.pass_pct || 0) > 0) passVals.push(Number(p.pass_pct || 0));
      if (Number(p.tackle_pct || 0) > 0) tackleVals.push(Number(p.tackle_pct || 0));
      if (Number(p.sofi_rating || 0) > 0) sofiVals.push(Number(p.sofi_rating || 0));
      if (Number(p.rating || 0) > 0) eaVals.push(Number(p.rating || 0));
    });
  });
  const games = Math.max((matches || []).length, 1);
  out.avg_players = +(out.player_apps / games).toFixed(1);
  out.shots_per_match = +(out.total_shots / games).toFixed(1);
  out.avg_pass_pct = +avgFrom(passVals).toFixed(1);
  out.avg_tackle_pct = +avgFrom(tackleVals).toFixed(1);
  out.avg_sofi = +avgFrom(sofiVals).toFixed(2);
  out.avg_ea = +avgFrom(eaVals).toFixed(2);
  return out;
}

function miniGeneralCard(label, value, tone='') {
  return `<div class="stat-card"><div class="stat-value ${tone}">${value}</div><div class="stat-label">${label}</div></div>`;
}

function renderVisao() {
  const matches = filteredMatches();
  const playersInClub = computePlayersForMatches(matches);
  const s = computeStatsFor(matches);
  const adv = computeAdvancedGeneralStats(matches);
  const wr = s.win_rate || 0;
  const passPct = adv.avg_pass_pct || 0;
  const tackPct = adv.avg_tackle_pct || 0;
  const offense = matches.length ? Math.min(100, Math.round(((s.goals_per_match || 0) * 18) + ((adv.shots_per_match || 0) * 4))) : 0;
  
  let html = `
    <div class="stats-grid">
      <div class="stat-card highlight">
        <div class="stat-value green">${wr}%</div>
        <div class="stat-label">Win Rate</div>
      </div>
      <div class="stat-card">
        <div class="stat-value green">${s.goals_for || 0}</div>
        <div class="stat-label">Gols Pró</div>
      </div>
      <div class="stat-card">
        <div class="stat-value red">${s.goals_against || 0}</div>
        <div class="stat-label">Gols Contra</div>
      </div>
      <div class="stat-card">
        <div class="stat-value green">${s.goal_diff || 0}</div>
        <div class="stat-label">Saldo</div>
      </div>
      <div class="stat-card">
        <div class="stat-value compound">
          <span class="green">${s.wins || 0}</span><span style="color:#555">/</span>
          <span class="yellow">${s.draws || 0}</span><span style="color:#555">/</span>
          <span class="red">${s.losses || 0}</span>
        </div>
        <div class="stat-label">${s.matches_played || 0} jogos</div>
      </div>
      <div class="stat-card">
        <div class="stat-value">${s.goals_per_match || 0}</div>
        <div class="stat-label">Gols/Jogo</div>
      </div>
      <div class="stat-card">
        <div class="stat-value">${adv.shots_per_match || 0}</div>
        <div class="stat-label">Chutes/Jogo</div>
      </div>
      <div class="stat-card">
        <div class="stat-value">${s.clean_sheets || 0}</div>
        <div class="stat-label">Sem Sofrer Gol</div>
      </div>
    </div>
    
    <div class="circles-grid">
      ${circle(wr, 'WIN RATE')}
      ${circle(passPct, '% PASSES')}
      ${circle(tackPct, '% DIVIDIDAS')}
      ${circle(offense, 'OFENSIVIDADE')}
    </div>
    
    <div class="stat-card highlight" style="display:flex;align-items:center;gap:16px;justify-content:flex-start;text-align:left;padding:20px;">
      <div style="font-size:32px;color:var(--yellow);">⭐</div>
      <div>
        <div style="font-size:28px;font-weight:800;color:var(--green);">${s.best_streak || 0}</div>
        <div class="stat-label">Melhor Sequência de Vitórias</div>
      </div>
    </div>
  `;

  html += `
    <div class="section-title">📌 Métricas Gerais do Clube no Filtro</div>
    <div class="stats-grid">
      ${miniGeneralCard('Média EA Elenco', adv.avg_ea || 0, 'green')}
      ${miniGeneralCard('Média Sofi', adv.avg_sofi || 0, 'green')}
      ${miniGeneralCard('Jogadores/Jogo', adv.avg_players || 0)}
      ${miniGeneralCard('Chutes Totais', adv.total_shots || 0)}
      ${miniGeneralCard('Pass% Médio', (adv.avg_pass_pct || 0) + '%')}
      ${miniGeneralCard('Des% Médio', (adv.avg_tackle_pct || 0) + '%')}
      ${miniGeneralCard('Desarmes', adv.tackles_made || 0)}
      ${miniGeneralCard('Defesas', adv.saves || 0)}
      ${miniGeneralCard('MOMs', adv.moms || 0)}
      ${miniGeneralCard('Vermelhos', adv.red_cards || 0, adv.red_cards ? 'red' : '')}
      ${miniGeneralCard('Jogos 3+ Gols', adv.games_3gf || 0, 'green')}
      ${miniGeneralCard('Sofreu 3+', adv.games_3ga || 0, adv.games_3ga ? 'red' : '')}
      ${miniGeneralCard('Melhor Ataque', adv.best_attack || 0, 'green')}
      ${miniGeneralCard('Pior Defesa', adv.worst_defense || 0, adv.worst_defense >= 3 ? 'red' : '')}
      ${miniGeneralCard('Jogos sem Marcar', adv.scoreless_games || 0, adv.scoreless_games ? 'yellow' : '')}
      ${miniGeneralCard('Sofreu Gol', adv.conceded_games || 0)}
    </div>
  `;
  
  // MVP calculado somente pelas partidas do clube/filtro atual
  const m = playersInClub.length ? [...playersInClub].sort((a,b) => {
    const scoreA = Number(a.mom || 0) * 25 + Number(a.rating || 0) * 10 + Number(a.goals || 0) * 1.5 + Number(a.assists || 0);
    const scoreB = Number(b.mom || 0) * 25 + Number(b.rating || 0) * 10 + Number(b.goals || 0) * 1.5 + Number(b.assists || 0);
    return scoreB - scoreA;
  })[0] : null;
  if (m) {
    html += `
      <div class="section-title">🏆 MVP do Clube no Filtro</div>
      <div class="mvp-card">
        <div class="mvp-badge">M.V.P.</div>
        <div class="mvp-rating">${m.rating}</div>
        <div class="mvp-info">
          <div class="mvp-name">${m.name}</div>
          <div class="mvp-meta">${m.position} · ${m.games} jogos no clube/filtro</div>
          <div class="mvp-stats">
            <div class="mvp-stat"><div class="mvp-stat-value">${m.goals}</div><div class="mvp-stat-label">Gols</div></div>
            <div class="mvp-stat"><div class="mvp-stat-value">${m.assists}</div><div class="mvp-stat-label">Assist</div></div>
            <div class="mvp-stat"><div class="mvp-stat-value">${m.mom}</div><div class="mvp-stat-label">MOM</div></div>
            <div class="mvp-stat"><div class="mvp-stat-value">${m.pass_pct}%</div><div class="mvp-stat-label">Pass%</div></div>
          </div>
        </div>
      </div>
    `;
  }
  
  const opponentsForFilter = calcOpponentAvgClient(matches);
  if (opponentsForFilter.length) {
    html += `
      <div class="section-title">📊 Média de Gols por Adversário</div>
      <div class="opponents-list">
    `;
    opponentsForFilter.forEach(o => {
      html += `
        <div class="opp-row">
          <div class="opp-name">${o.opponent}<span class="opp-name-small">${o.games}j</span></div>
          <div class="opp-stats">
            <div class="opp-stat"><span class="opp-stat-val green">${o.avg_gf}</span><span class="opp-stat-label">PRO</span></div>
            <div class="opp-stat"><span class="opp-stat-val red">${o.avg_ga}</span><span class="opp-stat-label">SOF</span></div>
          </div>
        </div>
      `;
    });
    html += '</div>';
  }
  
  // Desempenho por partida
  if (matches.length) {
    html += `
      <div class="section-title">📈 Desempenho por Partida</div>
      <div class="perf-bars">
    `;
    const recent = matches.slice(0, 12).reverse();
    const maxGoals = Math.max(...recent.map(m => m.goals_for + m.goals_against), 5);
    recent.forEach(m => {
      const cls = m.result === 'V' ? 'win' : m.result === 'D' ? 'loss' : 'draw';
      const totalH = ((m.goals_for + m.goals_against) / maxGoals) * 100;
      html += `
        <div class="perf-bar ${cls}">
          <div class="perf-bar-bar" style="height: ${Math.max(totalH, 20)}%;"></div>
          <div class="perf-bar-score">${m.score}</div>
          <div class="perf-bar-date">${m.date.slice(0,5)}</div>
        </div>
      `;
    });
    html += '</div>';
  }
  
  // Últimas partidas com MOM
  html += `
    <div class="section-title">⚔️ Últimas Partidas</div>
    <div class="matches-list">
  `;
  matches.slice(0, 10).forEach(m => {
    html += `
      <div class="match-card" onclick="showMatchDetails('${m.match_id}')">
        <div>
          <div class="match-opp">VS ${m.opponent.toUpperCase()}</div>
          ${m.mom ? `<div class="match-mom">MOM: <strong>${m.mom}</strong> (${m.mom_rating})</div>` : ''}
        </div>
        <div class="match-results">
          <span class="match-badge ${m.result.toLowerCase()}">${m.result}</span>
          <span class="match-score">${m.score}</span>
        </div>
      </div>
    `;
  });
  html += '</div>';
  
  return html;
}

function circle(pct, label) {
  const r = 50;
  const circ = 2 * Math.PI * r;
  const offset = circ - (pct / 100) * circ;
  return `
    <div class="circle-card">
      <div class="circle-svg">
        <svg viewBox="0 0 130 130" width="130" height="130">
          <circle class="circle-bg" cx="65" cy="65" r="${r}"></circle>
          <circle class="circle-progress" cx="65" cy="65" r="${r}"
            stroke-dasharray="${circ}" stroke-dashoffset="${offset}"></circle>
        </svg>
        <div class="circle-text">${pct}%</div>
      </div>
      <div class="circle-label">${label}</div>
    </div>
  `;
}

function playerStatLine(label, value) {
  return `<div class="player-stat"><span class="player-stat-label">${label}</span><span class="player-stat-val">${value ?? 0}</span></div>`;
}

function renderJogadores() {
  const players = scopedPlayers();
  if (!players.length) {
    return '<div class="empty-state">Nenhum jogador encontrado neste filtro</div>';
  }
  const scopeLabel = CURRENT_MATCH_TYPE === 'todos' ? 'todos os jogos do clube' : 'jogos de ' + CURRENT_MATCH_TYPE + ' no clube';
  let html = `<div class="section-title">Jogadores · ${scopeLabel}</div><div class="players-grid">`;
  players.forEach(p => {
    html += `
      <div class="player-card" onclick="showPlayerDetail('${p.name.replace(/'/g, "\\'")}')">
        <div class="player-rating-big">${p.rating}</div>
        <div class="player-pos">
          <span class="player-pos-badge">${p.position} · ${p.games}J no clube/tipo · ${p.position_source || 'auto'}</span>
        </div>
        <div class="player-name">${p.name}</div>
        <div class="player-stats">
          ${playerStatLine('Sofi', p.sofi_rating)}
          ${playerStatLine('Gols', p.goals)}
          ${playerStatLine('Assist', p.assists)}
          ${playerStatLine('G+A', p.goal_involvements)}
          ${playerStatLine('G/J', p.goals_per_game)}
          ${playerStatLine('A/J', p.assists_per_game)}
          ${playerStatLine('Chutes', p.shots)}
          ${playerStatLine('Chu/J', p.shots_per_game)}
          ${playerStatLine('Pass%', p.pass_pct + '%')}
          ${playerStatLine('Passes', p.passes_made)}
          ${playerStatLine('Des%', p.tackle_pct + '%')}
          ${playerStatLine('Desarmes', p.tackles_made)}
          ${playerStatLine('Defesas', p.saves)}
          ${playerStatLine('SG', p.clean_sheet)}
          ${playerStatLine('MOM', p.mom)}
          ${playerStatLine('V/E/D', `${p.wins}/${p.draws}/${p.losses}`)}
          ${playerStatLine('Win%', p.win_rate + '%')}
          ${playerStatLine('Verm.', p.reds)}
        </div>
      </div>
    `;
  });
  html += '</div>';
  return html;
}

function renderComparar() {
  const players = scopedPlayers();
  if (!players.length) {
    return '<div class="empty-state">Nenhum jogador encontrado</div>';
  }
  const opts = players.map(p =>
    `<option value="${p.name}">${p.name} · ${p.position} · ${p.rating}</option>`
  ).join('');
  if (!players.some(p => p.name === COMPARE_A)) COMPARE_A = players[0]?.name || null;
  if (!players.some(p => p.name === COMPARE_B)) COMPARE_B = players[1]?.name || players[0]?.name || null;
  const p1 = COMPARE_A || players[0]?.name;
  const p2 = COMPARE_B || players[1]?.name || players[0]?.name;
  const sel = (val) => opts.replace(`value="${val}"`, `value="${val}" selected`);

  return `
    <div class="compare-grid">
      <div class="compare-pick">
        <select onchange="setCompare('A', this.value)">${sel(p1)}</select>
        <div id="cmp-a-card"></div>
      </div>
      <div class="compare-pick">
        <select onchange="setCompare('B', this.value)">${sel(p2)}</select>
        <div id="cmp-b-card"></div>
      </div>
    </div>
    <div id="cmp-bars"></div>
  `;
}

function setCompare(side, name) {
  if (side === 'A') COMPARE_A = name; else COMPARE_B = name;
  renderCompareBars();
}

function renderCompareBars() {
  if (CURRENT_TAB !== 'comparar') return;
  const players = scopedPlayers();
  if (!players.some(p => p.name === COMPARE_A)) COMPARE_A = players[0]?.name || null;
  if (!players.some(p => p.name === COMPARE_B)) COMPARE_B = players[1]?.name || players[0]?.name || null;
  const a = players.find(p => p.name === COMPARE_A) || players[0];
  const b = players.find(p => p.name === COMPARE_B) || players[1] || a;
  if (!a || !b) return;

  function card(p) {
    return `
      <div style="text-align:center;padding:14px 0;">
        <div style="font-size:38px;font-weight:800;color:var(--green);text-shadow:0 0 18px var(--green-glow);">${p.rating}</div>
        <div style="margin-top:4px;color:var(--text-2);text-transform:uppercase;font-size:10px;letter-spacing:1px;">${p.position} · ${p.games}J no clube/tipo</div>
        <div style="font-weight:800;font-size:16px;margin-top:4px;">${p.name}</div>
      </div>
    `;
  }
  const ca = document.getElementById('cmp-a-card');
  const cb = document.getElementById('cmp-b-card');
  if (ca) ca.innerHTML = card(a);
  if (cb) cb.innerHTML = card(b);

  const fields = [
    {key:'rating', label:'Nota', max:10},
    {key:'goals', label:'Gols', max: Math.max(a.goals, b.goals, 1)},
    {key:'assists', label:'Assist', max: Math.max(a.assists, b.assists, 1)},
    {key:'pass_pct', label:'Pass%', max:100},
    {key:'tackle_pct', label:'Div%', max:100},
    {key:'shots', label:'Chutes', max: Math.max(a.shots, b.shots, 1)},
    {key:'mom', label:'MOMs', max: Math.max(a.mom, b.mom, 1)},
    {key:'goals_per_game', label:'Gol/J', max: Math.max(a.goals_per_game, b.goals_per_game, 0.5)},
  ];
  let html = '<div class="compare-bars"><div style="font-weight:700;margin-bottom:8px;">Comparativo direto</div>';
  fields.forEach(f => {
    const va = Number(a[f.key] || 0), vb = Number(b[f.key] || 0);
    const pa = Math.min(100, (va / f.max) * 100);
    const pb = Math.min(100, (vb / f.max) * 100);
    const wa = va > vb ? 'color:var(--green)' : '';
    const wb = vb > va ? 'color:var(--green)' : '';
    html += `
      <div class="compare-bar-row">
        <div class="compare-bar-val left" style="${wa}">${va}</div>
        <div class="compare-bar left"><div class="fill" style="width:${pa}%"></div></div>
        <div class="compare-bar-label">${f.label}</div>
        <div class="compare-bar right"><div class="fill" style="width:${pb}%"></div></div>
        <div class="compare-bar-val right" style="${wb}">${vb}</div>
      </div>
    `;
  });
  html += '</div>';
  const target = document.getElementById('cmp-bars');
  if (target) target.innerHTML = html;
}

function renderConfrontos() {
  const matches = filteredMatches();
  if (!matches.length) {
    return '<div class="empty-state">Nenhuma partida no período selecionado</div>';
  }
  let html = '<div class="confronts-grid">';
  matches.forEach(m => {
    const top = (m.players_ratings || [])[0];
    html += `
      <div class="confront-card" onclick="showMatchDetails('${m.match_id}')" style="cursor:pointer;">
        <div>
          <div class="confront-name">${m.date} · VS ${String(m.opponent || '').toUpperCase()}</div>
          <div style="font-size:11px;color:var(--text-2);margin-top:4px;">${m.match_type} · MOM ${m.mom || '-'} ${m.mom_rating ? '(' + m.mom_rating + ')' : ''}${top ? ' · Melhor Sofi: ' + top.name + ' ' + top.sofi_rating : ''}</div>
        </div>
        <div class="confront-vs">
          <span class="vs-tag ${m.result.toLowerCase()}">${m.result}</span>
          <span style="margin-left:8px;color:var(--text);font-weight:700;">${m.score}</span>
        </div>
      </div>
    `;
  });
  html += '</div>';
  return html;
}

function normalizePlayerFamily(pos) {
  pos = String(pos || '').toLowerCase();
  if (['goalkeeper','gk','gol','goleiro'].includes(pos)) return 'GK';
  if (['defender','def','cb','zagueiro','zag','lb','rb','lwb','rwb','lateral'].includes(pos)) return 'DEF';
  if (['midfielder','mid','meia','cm','cdm','cam','lm','rm','volante'].includes(pos)) return 'MID';
  if (['forward','fwd','atacante','st','cf','lw','rw','lf','rf','ponta'].includes(pos)) return 'FWD';
  return 'MID';
}

function familyToPositionLabel(family) {
  return {GK:'GK', DEF:'defender', MID:'midfielder', FWD:'forward'}[family] || 'midfielder';
}

function profileForPlayer(name) {
  return PLAYER_PROFILES[name] || PLAYER_PROFILES[String(name || '').trim()] || {};
}

function inferPlayerPositionIntel(player) {
  const profile = profileForPlayer(player.name);
  const counts = player.position_counts || {GK:0, DEF:0, MID:0, FWD:0};
  if (profile.manual_position) {
    const fam = normalizePlayerFamily(profile.manual_position);
    const totalApps = Number(player.games || player.history_apps || 0);
    const gkApps = Number(counts.GK || 0);
    const outfieldApps = Number(counts.DEF || 0) + Number(counts.MID || 0) + Number(counts.FWD || 0);
    // Protecao anti-erro: nao deixa um atacante virar GK por cadastro/localStorage antigo
    // se ele nunca jogou como goleiro no historico do clube.
    if (fam === 'GK' && totalApps > 0 && gkApps === 0 && outfieldApps > 0) {
      console.warn('Cadastro manual GK ignorado por ausencia de jogos como goleiro:', player.name);
    } else {
      return {family: fam, label: familyToPositionLabel(fam), source: 'manual', apps: totalApps, counts};
    }
  }

  let apps = Number(player.games || player.history_apps || 0);
  const registered = normalizePlayerFamily(player.favorite_position || player.position);
  const last = normalizePlayerFamily(player.last_match_position || '');
  let family = registered;
  let source = 'posição favorita EA';

  if (apps > 0) {
    const sorted = Object.entries(counts).sort((a,b) => b[1] - a[1]);
    const top = sorted[0];
    const topShare = top ? top[1] / Math.max(apps, 1) : 0;
    const gkShare = (counts.GK || 0) / Math.max(apps, 1);
    const maxOutfield = Math.max(counts.DEF || 0, counts.MID || 0, counts.FWD || 0);
    if (registered === 'GK' || ((counts.GK || 0) >= 2 && gkShare >= 0.5 && (counts.GK || 0) >= maxOutfield)) {
      family = 'GK';
      source = registered === 'GK' ? 'posição favorita EA' : 'histórico como GK';
    } else if (top && top[1] >= 2 && topShare >= 0.45 && top[0] !== 'GK') {
      family = top[0];
      source = 'últimos jogos';
    } else if (last && last !== 'GK') {
      family = last;
      source = 'último jogo';
    }
  }
  return {family, label: familyToPositionLabel(family), source, apps, counts};
}
const FORMATION_SLOTS = {
  '3-5-2': ['GK','LCB','CB','RCB','LM','LCM','CM','RCM','RM','LST','RST'],
  '4-3-3': ['GK','LB','LCB','RCB','RB','LCM','CM','RCM','LW','ST','RW'],
  '4-4-2': ['GK','LB','LCB','RCB','RB','LM','LCM','RCM','RM','LST','RST'],
  '4-2-3-1': ['GK','LB','LCB','RCB','RB','LDM','RDM','LAM','CAM','RAM','ST'],
  '4-1-2-1-2': ['GK','LB','LCB','RCB','RB','CDM','LCM','RCM','CAM','LST','RST'],
  '3-4-3': ['GK','LCB','CB','RCB','LM','LCM','RCM','RM','LW','ST','RW'],
  '5-3-2': ['GK','LWB','LCB','CB','RCB','RWB','LCM','CM','RCM','LST','RST'],
};

const ROLE_COORDS = {
  GK:[50,94], LB:[16,78], LWB:[12,66], LCB:[32,82], CB:[50,84], RCB:[68,82], RB:[84,78], RWB:[88,66],
  CDM:[50,66], LDM:[38,66], RDM:[62,66], LCM:[34,54], CM:[50,51], RCM:[66,54], LM:[14,48], RM:[86,48],
  LAM:[32,35], CAM:[50,33], RAM:[68,35], LW:[20,22], RW:[80,22], ST:[50,15], LST:[39,15], RST:[61,15],
};

const ROLE_DESC = {
  GK:'Goleiro - protege a meta e inicia a saída de bola.',
  LB:'Lateral esquerdo - amplitude, cobertura e apoio pela esquerda.', RB:'Lateral direito - amplitude, cobertura e apoio pela direita.',
  LWB:'Ala esquerdo - corredor inteiro, apoio ofensivo e recomposição.', RWB:'Ala direito - corredor inteiro, apoio ofensivo e recomposição.',
  LCB:'Zagueiro pela esquerda - cobertura e primeira construção.', CB:'Zagueiro central - lidera a linha defensiva.', RCB:'Zagueiro pela direita - cobertura e duelos laterais.',
  CDM:'Volante - protege a defesa e organiza a saída.', LDM:'Volante esquerdo - equilíbrio, cobertura e passe curto.', RDM:'Volante direito - equilíbrio, cobertura e pressão.',
  LCM:'Meia central esquerdo - conexão, apoio e chegada.', CM:'Meia central - dita ritmo e liga defesa/ataque.', RCM:'Meia central direito - conexão, apoio e chegada.',
  LM:'Meia/ala esquerdo - amplitude e criação pelo lado.', RM:'Meia/ala direito - amplitude e criação pelo lado.',
  CAM:'Meia ofensivo - cria chances entre linhas.', LAM:'Meia ofensivo esquerdo - corta para dentro e cria.', RAM:'Meia ofensivo direito - corta para dentro e cria.',
  LW:'Ponta esquerda - profundidade e finalização pelo lado.', RW:'Ponta direita - profundidade e finalização pelo lado.',
  ST:'Centroavante - referência, gols e ataque à área.', LST:'Atacante esquerdo - ataca espaços e combina por dentro.', RST:'Atacante direito - ataca espaços e combina por dentro.',
};

const ROLE_PREF = {
  GK:['GK'], LB:['DEF','MID'], RB:['DEF','MID'], LWB:['DEF','MID'], RWB:['DEF','MID'], LCB:['DEF'], CB:['DEF'], RCB:['DEF'],
  CDM:['MID','DEF'], LDM:['MID','DEF'], RDM:['MID','DEF'], LCM:['MID'], CM:['MID'], RCM:['MID'], LM:['MID','FWD'], RM:['MID','FWD'],
  CAM:['MID','FWD'], LAM:['MID','FWD'], RAM:['MID','FWD'], LW:['FWD','MID'], RW:['FWD','MID'], ST:['FWD'], LST:['FWD'], RST:['FWD'],
};

function roleScore(p, targetFamily) {
  const rating = Number(p.rating || 0) * 10;
  if (targetFamily === 'DEF') return rating + Number(p.tackle_pct || 0) * 0.08 + Number(p.mom || 0) * 0.05;
  if (targetFamily === 'MID') return rating + Number(p.assists_per_game || 0) * 4 + Number(p.pass_pct || 0) * 0.05;
  if (targetFamily === 'FWD') return rating + Number(p.goals_per_game || 0) * 5 + Number(p.assists_per_game || 0) * 2;
  return rating;
}

function buildIdealTeamClient(formation) {
  const slots = FORMATION_SLOTS[formation] || FORMATION_SLOTS['3-5-2'];
  const pool = scopedPlayers().map(p => { const intel = inferPlayerPositionIntel(p); return {...p, family: intel.family, position: intel.label, position_source: intel.source, position_counts: intel.counts, history_apps: intel.apps}; }).sort((a,b) => Number(b.rating || 0) - Number(a.rating || 0));
  const used = new Set();
  const picked = [];
  slots.forEach(slot => {
    const wanted = ROLE_PREF[slot] || ['MID'];
    let best = null;
    let bestScore = -999;
    pool.forEach(p => {
      if (used.has(p.name)) return;
      if (slot === 'GK' && p.family !== 'GK') return;
      let fitBonus = 0;
      if (p.family === wanted[0]) fitBonus = 18;
      else if (wanted.includes(p.family)) fitBonus = 9;
      else fitBonus = -18;
      // GK continua protegido. Nas outras posições, se faltar natural/adaptado,
      // completa o XI com o melhor jogador restante em vez de deixar buraco no campo.
      const score = roleScore(p, wanted[0]) + fitBonus;
      if (score > bestScore) { bestScore = score; best = p; }
    });
    if (best) {
      used.add(best.name);
      const [x,y] = ROLE_COORDS[slot] || [50,50];
      const fit = best.family === wanted[0] ? 'natural' : wanted.includes(best.family) ? 'adaptado' : 'improvisado';
      picked.push({...best, role:slot, field_pos:slot, x, y, fit, role_description: ROLE_DESC[slot], selection_score: Math.round(bestScore * 10) / 10});
    }
  });
  return {formation, formation_name:`Formação ${formation}`, slots, players:picked, missing_slots:slots.filter(s => !picked.some(p => p.role === s))};
}

function setIdealFormation(value) {
  IDEAL_FORMATION = value;
  if (!isAdmin()) {
    document.querySelectorAll('.tab').forEach(el => {
      if (['JOGADORES','COMPARAR','CONFRONTOS','ADVERSÁRIOS'].includes((el.textContent || '').trim())) el.remove();
    });
  }
  renderTab();
}

function playstyleIcon(nameOrCode) {
  const key = String(nameOrCode || '').toLowerCase();
  const map = {
    'chute forte':'💥', 'power shot':'💥', 'bola parada':'🎯', 'dead ball':'🎯', 'cavadinha':'🧤', 'chip shot':'🧤',
    'chute colocado':'🌀', 'finesse shot':'🌀', 'cabeceio forte':'🦅', 'power header':'🦅', 'acrobático':'🤸', 'acrobatic':'🤸',
    'chute rasteiro':'⬇', 'low driven shot':'⬇', 'decisivo':'⚡', 'gamechanger':'⚡', 'passe incisivo':'🧭', 'incisive pass':'🧭',
    'passe pingado':'➡', 'pinged pass':'➡', 'bola longa':'↗', 'long ball pass':'↗', 'tiki taka':'🔁', 'passe curvado':'〰', 'whipped pass':'〰',
    'inventivo':'🎩', 'inventive':'🎩', 'jóquei':'🕺', 'jockey':'🕺', 'bloqueio':'🚧', 'block':'🚧', 'interceptação':'🪝', 'intercept':'🪝',
    'antecipação':'🦊', 'anticipate':'🦊', 'carrinho':'🛝', 'slide tackle':'🛝', 'fortaleza aérea':'🛡', 'aerial fortress':'🛡',
    'técnico':'🎮', 'technical':'🎮', 'rápido':'💨', 'rapid':'💨', 'primeiro toque':'🧲', 'first touch':'🧲', 'malandro':'✨', 'trickster':'✨',
    'pressão provada':'🧱', 'press proven':'🧱', 'arranque':'🚀', 'quick step':'🚀', 'incansável':'♾', 'relentless':'♾', 'arremesso longo':'🙌', 'long throw':'🙌',
    'brutamontes':'💪', 'bruiser':'💪', 'protetor':'🥊', 'enforcer':'🥊', 'lançamento longo':'🎯', 'far throw':'🎯', 'jogo com os pés':'🦶', 'footwork':'🦶',
    'pegador de cruzamento':'🧤', 'cross claimer':'🧤', 'saída rápida':'🏃', 'rush out':'🏃', 'alcance longo':'🪽', 'far reach':'🪽', 'defletor':'🪞', 'deflector':'🪞'
  };
  return map[key] || '◆';
}

function archetypeIcon(name) {
  const key = String(name || '').toLowerCase();
  const map = {
    'chefia':'🛡', 'chefão':'🛡', 'chefao':'🛡', 'líbero':'↗', 'libero':'↗', 'progressor':'↗', 'cão de guarda':'🐕', 'cao de guarda':'🐕', 'saqueador':'⚡', 'muralha':'🧤',
    'regista':'🎼', 'motor':'⚙', 'armador':'🧠', 'box-to-box':'🔄', 'ponta veloz':'💨', 'camisa 10':'🔟',
    'finalizador':'🎯', 'homem alvo':'🗼', 'goleiro linha':'🧤', 'goleiro líbero':'🧤', 'gl-linha':'🧤', 'paredão':'🧱', 'ala profundo':'↕'
  };
  return map[key] || '◆';
}

function styleIconHtml(icon, plus=false) {
  return `<span class="style-icon ${plus ? 'plus' : ''}" aria-hidden="true"><span>${icon}</span></span>`;
}
function playstyleSelectOptions(selected='') {
  return ['<option value="">Estilo de jogo</option>'].concat(PLAYSTYLE_CATALOG.map(ps => `<option value="${ps.name}" ${selected === ps.name ? 'selected' : ''}>${ps.name}</option>`)).join('');
}

function renderCadastroJogadores() {
  const players = computePlayersForMatches(DATA.matches || []);
  const fallback = (DATA.players || []).map(p => {
    const base = {...p, favorite_position:p.position, position_counts:{GK:0,DEF:0,MID:0,FWD:0}, games:0};
    const intel = inferPlayerPositionIntel(base);
    return {...base, position:intel.label, position_source:intel.source};
  });
  const rows = (players.length ? players : fallback);
  const opts = [
    ['', 'AUTO'], ['GK','GK - Goleiro'], ['CB','CB - Zagueiro'], ['LB','LB - Lateral Esq.'], ['RB','RB - Lateral Dir.'],
    ['CDM','CDM - Volante'], ['CM','CM - Meia'], ['CAM','CAM - Meia Ofensivo'], ['LM','LM - Ala/Meia Esq.'], ['RM','RM - Ala/Meia Dir.'],
    ['LW','LW - Ponta Esq.'], ['RW','RW - Ponta Dir.'], ['ST','ST - Atacante']
  ];
  const rowsHtml = rows.map(p => {
    const profile = profileForPlayer(p.name);
    const intel = inferPlayerPositionIntel(p);
    const selectHtml = opts.map(([v,l]) => `<option value="${v}" ${String(profile.manual_position || '') === v ? 'selected' : ''}>${l}</option>`).join('');
    const selectedStyles = profile.playstyles || [];
    return `
      <div class="profile-row">
        <div>
          <div class="profile-name">${p.name}</div>
          <div class="profile-meta">EA favorita: ${p.favorite_position || p.position || '?'} - ultimo: ${p.last_match_position || '-'} - sugerida: ${intel.label} (${intel.source}) - ${p.games || 0}j no clube</div>
        </div>
        <select id="prof-pos-${cssSafeId(p.name)}">${selectHtml}</select>
        <input id="prof-arch-${cssSafeId(p.name)}" placeholder="Arquétipo" value="${escapeAttr(profile.archetype || '')}">
        <select id="prof-ps-1-${cssSafeId(p.name)}">${playstyleSelectOptions(selectedStyles[0] || '')}</select>
        <select id="prof-ps-2-${cssSafeId(p.name)}">${playstyleSelectOptions(selectedStyles[1] || '')}</select>
        <select id="prof-ps-3-${cssSafeId(p.name)}">${playstyleSelectOptions(selectedStyles[2] || '')}</select>
        <input id="prof-notes-${cssSafeId(p.name)}" placeholder="Notas" value="${escapeAttr(profile.notes || '')}">
        <button class="btn-mini" onclick="saveProfileFromRow('${p.name.replace(/'/g, "\'")}')">Salvar</button>
      </div>
    `;
  }).join('');
  return `
    <div class="section-title">Cadastro de Jogadores</div>
    <div style="color:var(--text-2);font-size:12px;margin-bottom:12px;line-height:1.5;">
      O script sugere posição pela posição favorita da EA e pelas posições dos últimos jogos. Se errar, ajuste aqui uma vez e o Time Ideal passa a obedecer. As estatísticas continuam sempre só do clube pesquisado.
    </div>
    <div class="profile-list">${rowsHtml}</div>
  `;
}

function cssSafeId(value) {
  return String(value || '').replace(/[^a-zA-Z0-9_-]/g, '_');
}

function escapeAttr(value) {
  return String(value || '').replace(/&/g, '&amp;').replace(/"/g, '&quot;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
}

async function saveProfileFromRow(name) {
  const id = cssSafeId(name);
  const pos = document.getElementById('prof-pos-' + id)?.value || '';
  const arch = document.getElementById('prof-arch-' + id)?.value || '';
  const notes = document.getElementById('prof-notes-' + id)?.value || '';
  const playstyles = [1,2,3].map(i => document.getElementById(`prof-ps-${i}-` + id)?.value || '').filter(Boolean);
  try {
    await savePlayerProfile(name, pos, arch, notes, playstyles);
    renderTab();
  } catch (e) {
    alert(e.message);
  }
}


function findPlaystyle(name) {
  return PLAYSTYLE_CATALOG.find(p => p.name === name || p.code === name) || {name, code:'', group:'', desc:'Estilo recomendado para complementar a função.'};
}

function findArchetype(name) {
  return ARCHETYPE_CATALOG.find(a => a.name === name) || {name, group:'', desc:'Perfil tático recomendado para a função descrita.'};
}

function uniqueStyleNames(names) {
  const seen = new Set();
  return names.filter(name => {
    if (!name || seen.has(name)) return false;
    seen.add(name);
    return true;
  });
}

function suggestBuildRecipe(position, text) {
  const inputText = String(text || '').trim();
  const t = inputText.toLowerCase();
  const has = (re) => re.test(t);
  const wants = {
    gk: has(/\b(gk|goleiro)\b|reflex|sair do gol|defesa com os pés|defesa com os pes|reposi|mãos|maos/),
    cb: has(/zague|defensor|defesa|marcar|antecip|desarme|xerife|chefia|chefe|cobertura|a[eé]re|cabe[cç]|combate/),
    fullback: has(/lateral|ala|cruz|corredor|linha de fundo|apoio|amplitude/),
    cdm: has(/volante|cdm|cão|cao|protege|marca|roubar|intercept|recupera/),
    creator: has(/cam|meia|criador|armador|passe|assist|maestro|10|achar passe|vis[aã]o/),
    winger: has(/ponta|drible|veloc|1x1|aberto|profundidade|arranque/),
    striker: has(/atac|st|gol|final|chute|artilheiro|matador|pivô|pivo|referencia|referência/),
  };
  let role = 'equilibrado';
  if (inputText) {
    if (wants.cb) role = 'zagueiro';
    else if (wants.cdm) role = 'volante';
    else if (wants.fullback) role = 'lateral';
    else if (wants.creator) role = 'criador';
    else if (wants.winger) role = 'ponta';
    else if (wants.striker) role = 'atacante';
    else if (wants.gk) role = 'goleiro';
  }

  const commonExplain = 'Os números abaixo são metas práticas de atributo, não custo exato de PA. No FC 26, cada Arquétipo tem mínimos/máximos próprios, atributos-chave mais baratos e custo crescente conforme o atributo sobe. Por isso a regra é: primeiro atingir os atributos que liberam/fortalecem a função, depois completar conforto e luxo.';
  const recipes = {
    goleiro: {
      archetype: has(/linha|sair|pé|pe|reposi/) ? 'Goleiro Líbero' : 'Paredão',
      main: ['Defesa com os Pés','Alcance Longo','Saída Rápida'],
      silver: ['Pegador de Cruzamento','Reposição Longa','Incansável','Primeiro Toque','Passe Pingado','Lançamento Longo','Resistente à Pressão','Jogo Aéreo'],
      why: 'Goleiro precisa primeiro defender. Se o texto pede saída, a build vira mais líbero; se não, prioriza reflexo e segurança.',
      attributes: [
        ['Goleiro','Reflexos','92-95','1','Base para defesas rápidas e 1x1.'], ['Goleiro','Alcance/Posicionamento','90-94','1','Ajuda em chutes colocados e bolas cruzadas.'], ['Goleiro','Jogo com os pés','80-86','2','Para repor curto e iniciar jogadas.'], ['Físico','Reação','88-92','2','Melhora resposta em bola viva.'], ['Passe','Passe curto','75-82','3','Só o bastante para não entregar saída.']
      ],
      phases: ['Primeiro suba atributos de defesa de goleiro até 90+.', 'Depois invista em reação e reposição.', 'Só gaste em passe/controle se o time realmente usa saída curta.']
    },
    zagueiro: {
      archetype: 'Chefia',
      main: ['Antecipação','Interceptação','Bloqueio'],
      silver: ['Jogo Aéreo','Contenção','Brigador','Carrinho','Incansável','Passe Pingado','Lançamento Longo','Resistente à Pressão'],
      why: 'Zagueiro competitivo precisa parar jogada antes do chute, ganhar duelo físico e ainda sair simples quando recuperar a bola.',
      attributes: [
        ['Defesa','Interceptação','90-94','1','Corta passe antes de virar chance clara.'], ['Defesa','Cabeceio','90-94','1','Ganha bola aérea defensiva e vira ameaça no escanteio.'], ['Defesa','Noção defensiva','90-94','1','Mantém posicionamento e leitura da linha.'], ['Defesa','Dividida em pé','88-92','1','Desarme principal; não deixe baixo.'], ['Físico','Força','90-95','1','Sustenta contato e disputa corporal.'], ['Físico','Impulsão','88-93','2','Combina com cabeceio para dominar bolas altas.'], ['Físico','Combatividade','88-92','2','Ajuda pressão, choque e recuperação.'], ['Ritmo','Aceleração/Pique','86-90','2','Suficiente para cobrir profundidade sem torrar tudo.'], ['Passe','Passe curto','78-84','3','Saída simples após recuperar.'], ['Passe','Lançamento','80-86','3','Virada e bola longa quando houver tempo.'], ['Controle','Agilidade/Reação','84-90','3','Virar o corpo e responder rápido sem virar build de meia.']
      ],
      phases: ['Feche primeiro Defesa + Força: interceptação, cabeceio, noção defensiva, dividida em pé e força.', 'Depois busque impulsão, combatividade e ritmo até uma faixa segura.', 'Por último, coloque passe curto/lançamento para sair jogando sem sacrificar a identidade defensiva.']
    },
    volante: {
      archetype: has(/box|chegar|ida|volta|motor/) ? 'Motor' : 'Cão de Guarda',
      main: ['Interceptação','Antecipação','Incansável'],
      silver: ['Passe Pingado','Tiki-Taka','Resistente à Pressão','Contenção','Brigador','Lançamento Longo','Primeiro Toque','Bloqueio'],
      why: 'Volante bom recupera, protege a zaga e entrega limpo. Não adianta roubar se perde a bola no passe seguinte.',
      attributes: [
        ['Defesa','Interceptação','88-92','1','Corta passe por dentro.'], ['Defesa','Dividida em pé','86-90','1','Bote seguro.'], ['Passe','Passe curto','84-90','1','Saída limpa sob pressão.'], ['Passe','Visão','82-88','2','Acha passe vertical.'], ['Físico','Fôlego','88-94','1','Mantém pressão o jogo todo.'], ['Físico','Combatividade','86-92','2','Ganha segunda bola.'], ['Controle','Reação','84-90','2','Decide rápido após recuperar.'], ['Ritmo','Aceleração','82-88','3','Para cobrir lados curtos.']
      ],
      phases: ['Primeiro defesa, passe curto e fôlego.', 'Depois visão/reação para acelerar transição.', 'Finalize com ritmo e físico extra.']
    },
    lateral: {
      archetype: 'Ala Criador',
      main: ['Cruzamento Tenso','Incansável','Arranque'],
      silver: ['Rápido com Bola','Passe Pingado','Tiki-Taka','Interceptação','Contenção','Primeiro Toque','Lançamento Longo','Jogo Aéreo'],
      why: 'Lateral precisa repetir corredor: defender, correr, cruzar e voltar. A build deve evitar ficar boa só atacando.',
      attributes: [
        ['Ritmo','Aceleração/Pique','88-92','1','Corredor inteiro.'], ['Físico','Fôlego','90-95','1','Repetição de sprint.'], ['Passe','Cruzamento','84-90','1','Entrega pelo lado.'], ['Defesa','Dividida em pé','82-88','2','Não ser avenida.'], ['Defesa','Interceptação','82-88','2','Cortar passe lateral.'], ['Controle','Condução','82-88','2','Avançar sem perder bola.'], ['Passe','Passe curto','80-86','3','Tabela por fora.']
      ],
      phases: ['Priorize ritmo, fôlego e cruzamento.', 'Depois suba defesa básica.', 'Finalize com condução e passe curto.']
    },
    criador: {
      archetype: has(/ritmo|controle|maestro|cm|meio/) ? 'Maestro' : 'Camisa 10',
      main: ['Passe Incisivo','Tiki-Taka','Resistente à Pressão'],
      silver: ['Primeiro Toque','Lançamento Longo','Passe Pingado','Técnico','Incansável','Trivela','Chute Colocado','Cruzamento Tenso'],
      why: 'Criador precisa receber pressionado, virar o corpo e transformar posse em chance clara.',
      attributes: [
        ['Passe','Visão','88-94','1','Passe que quebra linha.'], ['Passe','Passe curto','88-94','1','Tabela e posse segura.'], ['Passe','Lançamento','84-90','2','Inversão e bola longa.'], ['Controle','Controle de bola','86-92','1','Receber sob pressão.'], ['Controle','Reação','86-92','2','Decidir rápido.'], ['Controle','Agilidade/Equilíbrio','84-90','2','Girar e proteger.'], ['Finalização','Chute de longe','75-84','3','Punir espaço na entrada da área.']
      ],
      phases: ['Primeiro visão, passe curto e controle.', 'Depois reação/agilidade para jogar pressionado.', 'Finalize com lançamento e chute de longe se sobrar PA.']
    },
    ponta: {
      archetype: 'Ponta Agudo',
      main: ['Rápido com Bola','Técnico','Arranque'],
      silver: ['Primeiro Toque','Chute Colocado','Cruzamento Tenso','Passe Incisivo','Trivela','Incansável','Driblador','Resistente à Pressão'],
      why: 'Ponta útil vence 1x1 e decide depois: cruzar, tocar ou finalizar.',
      attributes: [
        ['Ritmo','Aceleração/Pique','90-95','1','Separar do marcador.'], ['Controle','Condução','88-94','1','Carregar em velocidade.'], ['Controle','Agilidade/Equilíbrio','88-94','1','Corte seco e mudança de direção.'], ['Passe','Cruzamento','82-88','2','Bola final pelo lado.'], ['Finalização','Finalização','80-88','2','Atacar diagonal.'], ['Passe','Passe curto','78-84','3','Tabela curta.'], ['Físico','Fôlego','84-90','3','Repetir corrida.']
      ],
      phases: ['Primeiro ritmo e condução.', 'Depois finalização/cruzamento conforme seu lado.', 'Finalize com passe curto e fôlego.']
    },
    atacante: {
      archetype: has(/pivo|refer|alto|cabe/) ? 'Referência' : has(/criar|falso|sair da area|sair da área/) ? 'Falso 9' : 'Matador',
      main: ['Chute Colocado','Chute Forte','Primeiro Toque'],
      silver: ['Cabeceio Forte','Trivela','Passe Incisivo','Rápido com Bola','Técnico','Resistente à Pressão','Acrobático','Incansável'],
      why: 'Atacante precisa transformar poucas chances em gol e dominar rápido dentro da área.',
      attributes: [
        ['Finalização','Finalização','90-95','1','Chance clara precisa virar gol.'], ['Finalização','Posicionamento','88-94','1','Atacar espaço certo.'], ['Finalização','Força do chute','86-92','1','Finalizar com potência.'], ['Controle','Primeiro toque/controle','84-90','2','Dominar e bater rápido.'], ['Ritmo','Aceleração/Pique','86-92','2','Atacar profundidade.'], ['Físico','Força','78-88','3','Segurar zagueiro se fizer pivô.'], ['Passe','Passe curto','75-82','3','Parede e tabela.']
      ],
      phases: ['Primeiro finalização, posicionamento e força do chute.', 'Depois primeiro toque e ritmo.', 'Só então coloque passe/físico conforme seu estilo.']
    },
    equilibrado: {
      archetype: 'Motor',
      main: ['Primeiro Toque','Incansável','Tiki-Taka'],
      silver: ['Passe Pingado','Resistente à Pressão','Interceptação','Arranque','Técnico','Passe Incisivo','Lançamento Longo','Chute Colocado'],
      why: 'Quando a descrição está aberta, a build segura é um jogador completo, útil e sem desperdício pesado em atributo de luxo.',
      attributes: [
        ['Controle','Reação','84-90','1','Base universal para responder rápido.'], ['Passe','Passe curto','84-90','1','Não quebrar jogada simples.'], ['Físico','Fôlego','86-92','1','Participar o jogo todo.'], ['Ritmo','Aceleração','84-90','2','Chegar antes no lance.'], ['Defesa','Interceptação','78-86','2','Ajudar sem bola.'], ['Controle','Controle de bola','82-88','2','Receber sob pressão.']
      ],
      phases: ['Monte uma base segura: reação, passe curto e fôlego.', 'Depois ajuste ritmo/controle.', 'Por fim especialize conforme a função que o time mais precisa.']
    }
  };

  let recipe = recipes[role] || recipes.equilibrado;
  if (role === 'zagueiro') {
    const wantsChief = has(/chefia|chefe|boss|lider|xerife|comando|capitao|capit[aã]o|organiza/);
    const wantsAerialWall = has(/a[eé]re|area|aereo|altura|cabe[cç]|for[cç]a|combate|fisic|disputa|duelo/);
    const wantsBuildUp = has(/sair jogando|sa[ií]da|constru|passe|lan[cç]amento|virada|bola longa/);
    if (wantsChief) {
      recipe = {...recipe, archetype:'Chefia', main:['Antecipação','Interceptação','Bloqueio'], why:'O texto pede zagueiro de comando. Chefia prioriza leitura, organização da linha, interceptação e bloqueio; use físico e cabeceio como sustentação.'};
    } else if (wantsAerialWall) {
      recipe = {...recipe, archetype:'Chefia', main:['Jogo Aéreo','Antecipação','Brigador'], why:'O texto descreve um zagueiro forte, líder, dominante em duelos e jogo aéreo. A base correta é Chefia/Chefão: comandar a defesa, proteger a área e vencer contato; saída de bola entra como complemento, não como identidade principal.'};
    } else if (wantsBuildUp) {
      recipe = {...recipe, archetype:'Líbero', main:['Antecipação','Passe Pingado','Lançamento Longo'], why:'O texto pede zagueiro que sai jogando. Líbero mantém leitura defensiva, mas investe mais cedo em passe rasteiro forte e bola longa.'};
    }
  }
  const mainNames = uniqueStyleNames(recipe.main).slice(0, 3);
  const silverNames = uniqueStyleNames([...recipe.silver, ...Object.values(recipes).flatMap(r => r.silver)]).filter(x => !mainNames.includes(x)).slice(0, 8);
  const archetype = findArchetype(recipe.archetype);
  return {
    role,
    archetype,
    main: mainNames.map(findPlaystyle),
    silver: silverNames.map(findPlaystyle),
    why: recipe.why,
    attributes: recipe.attributes.map(([group,name,target,priority,reason]) => ({group,name,target,priority,reason})),
    phases: recipe.phases,
    explanation: commonExplain,
    practical: 'Preencha de cima para baixo. Não tente colocar tudo em 95: depois de 88-90 o custo sobe muito. Pare nas faixas alvo, garanta os PlayStyles necessários e use o resto para corrigir fraquezas reais da função.'
  };
}
function suggestPlaystylesLocal(position, text) {
  return suggestBuildRecipe(position, text).main;
}

function runPlaystyleSimulator() {
  const txt = document.getElementById('sim-text')?.value || '';
  const build = suggestBuildRecipe('', txt);
  const mainHtml = build.main.map((p, i) => `<div class="build-card primary">
    <div class="style-card-head">${styleIconHtml(playstyleIcon(p.name))}<div><div class="style-card-title">${i+1}. ${p.name}</div><div class="style-card-code">Principal/ouro · ${p.code || ''} · ${p.group}</div></div></div>
    <div class="build-card-desc">${p.desc}</div>
  </div>`).join('');
  const silverHtml = build.silver.map((p, i) => `<div class="build-card">
    <div class="style-card-head">${styleIconHtml(playstyleIcon(p.name), true)}<div><div class="style-card-title">${i+1}. ${p.name}</div><div class="style-card-code">Complementar/prata · ${p.code || ''} · ${p.group}</div></div></div>
    <div class="build-card-desc">${p.desc}</div>
  </div>`).join('');
  const attrHtml = (build.attributes || []).map(a => `<tr>
    <td><span class="scout-pill">${a.group}</span></td>
    <td><strong>${a.name}</strong></td>
    <td><span class="attr-target">${a.target}</span></td>
    <td><span class="attr-priority">P${a.priority}</span></td>
    <td>${a.reason}</td>
  </tr>`).join('');
  const phasesHtml = (build.phases || []).map((p, i) => `<div class="attr-phase"><div class="attr-phase-title">Etapa ${i+1}</div><p>${p}</p></div>`).join('');
  document.getElementById('sim-result').innerHTML = `
    <div class="section-title" style="grid-column:1/-1;margin-top:8px;">Arquétipo recomendado</div>
    <div class="build-card primary" style="grid-column:1/-1;">
      <div class="style-card-head">${styleIconHtml(archetypeIcon(build.archetype.name))}<div><div class="style-card-title">${build.archetype.name}</div><div class="style-card-code">${build.archetype.group} · função detectada: ${build.role}</div></div></div>
      <div style="color:var(--text-2);font-size:13px;line-height:1.55;">${build.archetype.desc}</div>
      <div class="build-why">Por que: ${build.why}</div>
    </div>
    <div class="section-title" style="grid-column:1/-1;">3 PlayStyles principais/ouro</div>
    ${mainHtml}
    <div class="section-title" style="grid-column:1/-1;">8 PlayStyles complementares/prata</div>
    ${silverHtml}
    <div class="section-title" style="grid-column:1/-1;">Qualidades alvo</div>
    <div class="build-explain"><strong>Como ler:</strong> ${build.explanation}</div>
    <div class="attr-build-table-wrap"><table class="attr-build-table"><thead><tr><th>Grupo</th><th>Qualidade</th><th>Meta</th><th>Prioridade</th><th>Por que investir</th></tr></thead><tbody>${attrHtml}</tbody></table></div>
    <div class="section-title" style="grid-column:1/-1;">Ordem para gastar os pontos</div>
    <div class="attr-phase-grid">${phasesHtml}</div>
    <div class="build-card" style="grid-column:1/-1;"><div class="style-card-title">Como montar sem desperdiçar PA</div><div class="build-note" style="margin-top:8px;">${build.practical}</div></div>
  `;
}
function renderPlaystyles() {
  const groups = {};
  PLAYSTYLE_CATALOG.forEach(ps => { if (!groups[ps.group]) groups[ps.group] = []; groups[ps.group].push(ps); });
  const legend = Object.entries(groups).map(([group, list]) => `
    <div class="section-title">${group}</div>
    <div class="players-grid">
      ${list.map(ps => `<div class="player-card" style="cursor:default;">
        <div class="style-card-group">${ps.group}</div>
        <div class="style-card-head">${styleIconHtml(playstyleIcon(ps.name))}${styleIconHtml(playstyleIcon(ps.name), true)}<div><div class="style-card-title">${ps.name}</div><div class="style-card-code">${ps.code || ''}</div></div></div>
        <div style="color:var(--text-2);font-size:12px;line-height:1.5;">${ps.desc}</div>
      </div>`).join('')}
    </div>
  `).join('');
  const archGroups = {};
  ARCHETYPE_CATALOG.forEach(a => { if (!archGroups[a.group]) archGroups[a.group] = []; archGroups[a.group].push(a); });
  const archetypes = Object.entries(archGroups).map(([group, list]) => `
    <div class="section-title">Arquétipos - ${group}</div>
    <div class="players-grid">
      ${list.map(a => `<div class="player-card" style="cursor:default;">
        <div class="style-card-group">${a.group}</div>
        <div class="style-card-head">${styleIconHtml(archetypeIcon(a.name))}<div><div class="style-card-title">${a.name}</div><div class="style-card-code">Arquétipo</div></div></div>
        <div style="color:var(--text-2);font-size:12px;line-height:1.5;">${a.desc}</div>
      </div>`).join('')}
    </div>
  `).join('');
  return `
    <div class="section-title">Simulador de Arquétipo, Estilos e Qualidades</div>
    <div style="color:var(--text-2);font-size:12px;margin-bottom:12px;line-height:1.55;">Escreva do jeito que você falaria para um colega: posição, função, pontos fortes desejados e o que o jogador precisa fazer em campo. Exemplo: "zagueiro Chefia forte no jogo aéreo, bom desarme, saída simples e força para combate".</div>
    <div class="agenda-form" style="grid-template-columns:repeat(6,1fr);">
      <textarea id="sim-text" style="grid-column:span 6;min-height:92px;" placeholder="Descreva o jogador que você quer montar: zagueiro Chefia forte no jogo aéreo e desarme; volante que rouba e passa simples; atacante matador rápido; meia que cria e protege a bola..."></textarea>
      <div class="full"><button type="button" class="btn-primary" onclick="runPlaystyleSimulator()">Sugerir build completo</button></div>
    </div><div id="sim-result" class="build-result-grid"></div>
    <div class="section-title">Legenda de Arquétipos</div>
    <div style="color:var(--text-2);font-size:12px;margin-bottom:12px;line-height:1.5;">Arquétipo é o perfil tático/manual do jogador no seu elenco. Ele ajuda a IA, a análise scout e o Time Ideal a entenderem a função real do jogador, mesmo quando a API da EA erra a posição.</div>
    ${archetypes}
    <div class="section-title">Legenda de Estilos de Jogo do Pro Clubs</div>
    ${legend}
  `;
}

function renderTimeIdeal() {
  if (!scopedPlayers().length) {
    return '<div class="empty-state">Nenhum jogador com partidas neste clube/filtro para montar o time ideal</div>';
  }

  const team = buildIdealTeamClient(IDEAL_FORMATION);
  const formation = team.formation;
  const players = team.players || [];
  const formations = Object.keys(FORMATION_SLOTS);

  let fieldHtml = '';
  players.forEach((p, idx) => {
    const shirtNo = p.role === 'GK' ? 1 : idx + 1;
    const displayName = p.name.length > 14 ? p.name.slice(0, 14) : p.name;
    fieldHtml += `
      <div class="player-on-field" style="left:${p.x}%;top:${p.y}%;" title="${p.role}: ${p.role_description}">
        <div class="player-jersey ${p.role === 'GK' ? 'gk' : ''}">${shirtNo}</div>
        <div class="player-circle-name">${displayName}</div>
        <div class="player-rating-label">EA ${p.rating}</div>
        <div class="player-role-label">${p.role}</div>
      </div>
    `;
  });

  const listHtml = players.map((p, idx) => `
    <div class="ideal-row" style="display:grid;grid-template-columns:42px 70px 1fr 90px;gap:10px;align-items:center;padding:10px 12px;border-bottom:1px solid var(--border);">
      <div style="color:var(--text-3);font-size:11px;">${String(idx + 1).padStart(2,'0')}</div>
      <div><span class="tag ${p.fit === 'natural' ? 'liga' : p.fit === 'adaptado' ? 'copa' : 'd'}">${p.role}</span></div>
      <div>
        <div style="font-weight:800;">${p.name} <span style="color:var(--green);font-weight:800;">${p.rating}</span></div>
        <div style="color:var(--text-2);font-size:11px;line-height:1.4;">${p.role_description}</div>
        <div style="color:var(--text-3);font-size:10px;margin-top:2px;">Origem: ${p.position} · encaixe: ${p.fit}</div>
      </div>
      <div style="text-align:right;color:var(--text-2);font-size:11px;">score<br><strong style="color:var(--green);">${p.selection_score}</strong></div>
    </div>
  `).join('');

  return `
    <div style="display:flex;gap:10px;align-items:center;justify-content:center;margin-bottom:16px;flex-wrap:wrap;">
      <div class="formation-title" style="margin:0;">${team.formation_name}</div>
      <select onchange="setIdealFormation(this.value)" style="background:var(--bg-card);color:var(--text);border:1px solid var(--green-dim);border-radius:8px;padding:10px 12px;font-weight:700;">
        ${formations.map(f => `<option value="${f}" ${f === formation ? 'selected' : ''}>${f}</option>`).join('')}
      </select>
      <button class="btn-primary" style="padding:10px 16px;" onclick="renderTab()">Ajustar Melhor 11</button>
    </div>

    <div class="formation-wrapper">
      <div class="formation-title">${team.formation_name} · ${players.length}/11 jogadores</div>
      <div class="field">
        <div class="field-line"></div>
        <div class="field-circle"></div>
        <div class="field-spot top"></div>
        <div class="field-spot bottom"></div>
        ${fieldHtml}
      </div>
      <div class="formation-label">Escolha automática por função, nota e encaixe posicional</div>
    </div>

    <div class="section-title">Escalação e Função Tática</div>
    <div style="background:var(--bg-card);border:1px solid var(--border);border-radius:12px;overflow:hidden;margin-bottom:18px;">
      ${listHtml}
    </div>

    ${team.missing_slots.length ? `<div style="color:var(--yellow);font-size:12px;margin-bottom:14px;">Atenção: faltou jogador para ${team.missing_slots.join(', ')}. Se houver menos de 11 no filtro, mude para TODOS ou ajuste o cadastro.</div>` : ''}

    <div class="section-title">Análise Tática</div>
    <button class="btn-primary" onclick="analyzeTeam()">Gerar Análise com IA</button>
  `;
}

function renderAdversarios() {
  const results = renderOpponentScouts();
  return `
    <div class="section-title">Próximos Adversários</div>
    <div style="color:var(--text-2);font-size:12px;line-height:1.5;margin-bottom:12px;">Cadastre até 5 clubes rivais para uma leitura rápida: força do time, estilo provável, principais jogadores, pontos fortes/fracos e plano de jogo.</div>
    <div class="opponent-form">
      <input class="opp-scout-input" type="text" placeholder="Clube adversário 1">
      <input class="opp-scout-input" type="text" placeholder="Clube adversário 2">
      <input class="opp-scout-input" type="text" placeholder="Clube adversário 3">
      <input class="opp-scout-input" type="text" placeholder="Clube adversário 4">
      <input class="opp-scout-input" type="text" placeholder="Clube adversário 5">
      <div class="full"><button type="button" class="btn-primary" id="oppScoutBtn" onclick="scoutOpponents()">Analisar adversários</button></div>
    </div>
    <div id="opponentScoutStatus" style="color:var(--text-2);font-size:12px;margin-bottom:10px;"></div>
    <div id="opponentScoutResults" class="opponent-results">${results}</div>
  `;
}

function renderOpponentScouts() {
  if (!OPPONENT_SCOUTS || !OPPONENT_SCOUTS.length) return '<div class="empty-state" style="padding:44px 20px;"><div class="empty-text">Nenhum adversário analisado ainda.</div></div>';
  return OPPONENT_SCOUTS.map(o => {
    if (!o.found) return `<div class="opponent-card"><div class="opponent-name">${escapeAttr(o.name || 'Clube')}</div><div class="empty-text" style="margin-top:6px;">${escapeAttr(o.error || 'Não encontrado')}</div></div>`;
    const s = o.stats || {};
    const top = (o.top_players || []).slice(0, 5).map((p, i) => `${i+1}. ${escapeAttr(p.name)} (${escapeAttr(p.position || '-')}) · nota ${escapeAttr(p.rating)} · ${escapeAttr(p.goals || 0)}G/${escapeAttr(p.assists || 0)}A`).join('<br>') || 'Sem jogadores retornados pela EA';
    const strengths = (o.strengths || []).map(x => `<li>${escapeAttr(x)}</li>`).join('');
    const weaknesses = (o.weaknesses || []).map(x => `<li>${escapeAttr(x)}</li>`).join('');
    return `
      <div class="opponent-card">
        <div class="opponent-head"><div><div class="opponent-name">${escapeAttr(o.name)}</div><div class="opponent-style"><span class="scout-pill">${escapeAttr(o.style)}</span> · ID ${escapeAttr(o.club_id)} · ${escapeAttr(o.platform)}</div></div><div class="opponent-grade"><div class="rank">${escapeAttr(o.rank)}</div><div class="score">${escapeAttr(o.grade)}/100</div></div></div>
        <div class="opponent-table-wrap"><table class="opponent-table"><thead><tr><th>Jogos</th><th>V/E/D</th><th>Win</th><th>Gols</th><th>G/J</th><th>Sofre/J</th><th>Saldo</th><th>Top 5 jogadores</th></tr></thead><tbody><tr><td>${escapeAttr(s.matches || 0)}</td><td>${escapeAttr(s.wins || 0)} / ${escapeAttr(s.draws || 0)} / ${escapeAttr(s.losses || 0)}</td><td>${escapeAttr(s.win_rate || 0)}%</td><td>${escapeAttr(s.goals_for || 0)}-${escapeAttr(s.goals_against || 0)}</td><td>${escapeAttr(s.goals_per_match || 0)}</td><td>${escapeAttr(s.goals_against_per_match || 0)}</td><td>${escapeAttr(s.goal_diff || 0)}</td><td>${top}</td></tr></tbody></table></div>
        <div class="scout-cols"><div class="scout-box"><h4>Pontos fortes</h4><ul>${strengths}</ul></div><div class="scout-box"><h4>Pontos fracos</h4><ul>${weaknesses}</ul></div><div class="scout-box"><h4>Estratégia sugerida</h4><p>${escapeAttr(o.strategy || '')}</p></div></div>
      </div>`;
  }).join('');
}

async function scoutOpponents() {
  const names = Array.from(document.querySelectorAll('.opp-scout-input')).map(i => i.value.trim()).filter(Boolean).slice(0, 5);
  const status = document.getElementById('opponentScoutStatus');
  const btn = document.getElementById('oppScoutBtn');
  if (!names.length) { if (status) status.textContent = 'Informe pelo menos um clube adversário.'; return; }
  if (status) status.textContent = 'Buscando clubes na EA e montando scouting...';
  if (btn) { btn.disabled = true; btn.textContent = 'Analisando...'; }
  try {
    const r = await authFetch('/api/opponents/scout', { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({names, platform: 'auto'}) });
    if (!r.ok) throw new Error('Erro ao analisar adversários');
    const data = await r.json();
    OPPONENT_SCOUTS = data.opponents || [];
    const target = document.getElementById('opponentScoutResults');
    if (target) target.innerHTML = renderOpponentScouts();
    if (status) status.textContent = `${OPPONENT_SCOUTS.length} adversário(s) analisado(s).`;
  } catch (e) {
    if (status) status.textContent = 'Erro: ' + e.message;
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = 'Analisar adversários'; }
  }
}
function renderAgenda() {
  return `
    <div class="section-title">✏️ ${AGENDA_EDIT_ID ? 'Editar Agendamento' : 'Novo Agendamento'}</div>
    <form class="agenda-form" onsubmit="saveAgenda(event)">
      <input id="ag-opp" type="text" placeholder="Adversário" required style="grid-column: span 3;">
      <input id="ag-date" type="date" required style="grid-column: span 2;">
      <input id="ag-time" type="time" style="grid-column: span 1;">
      <select id="ag-type" style="grid-column: span 2;">
        <option value="liga">Liga</option>
        <option value="copa">Copa</option>
        <option value="amistoso">Amistoso</option>
      </select>
      <input id="ag-loc" type="text" placeholder="Local (opcional)" style="grid-column: span 4;">
      <textarea id="ag-notes" placeholder="Observações (opcional)"></textarea>
      <div class="full">
        ${AGENDA_EDIT_ID ? '<button type="button" class="btn-mini" onclick="cancelAgendaEdit()">Cancelar</button>' : ''}
        <button type="submit" class="btn-primary" style="padding:8px 18px;">${AGENDA_EDIT_ID ? 'Salvar Alterações' : 'Adicionar'}</button>
      </div>
    </form>
    <div class="section-title">📅 Próximos Jogos</div>
    <div id="agenda-list" class="agenda-list">${renderAgendaList()}</div>
  `;
}

function renderAgendaList() {
  if (!AGENDA.length) {
    return '<div class="empty-state" style="padding:40px 20px;"><div class="empty-text">Nenhum jogo agendado ainda.</div></div>';
  }
  const meses = ['JAN','FEV','MAR','ABR','MAI','JUN','JUL','AGO','SET','OUT','NOV','DEZ'];
  return AGENDA.map(a => {
    const d = new Date(a.match_date + 'T00:00:00');
    const dia = d.getDate();
    const mes = meses[d.getMonth()];
    const hora = a.match_time || '--:--';
    return `
      <div class="agenda-row">
        <div class="agenda-date">
          <div class="d">${dia}</div>
          <div class="m">${mes}</div>
        </div>
        <div class="agenda-info">
          <div class="opp">VS ${a.opponent}</div>
          <div class="meta">${hora} · <span class="tag ${a.match_type}">${a.match_type}</span> ${a.location ? '· ' + a.location : ''}</div>
          ${a.notes ? '<div class="meta" style="margin-top:4px;">✍️ ' + a.notes + '</div>' : ''}
        </div>
        <button class="btn-mini" onclick="editAgenda(${a.id})">Editar</button>
        <button class="btn-mini danger" onclick="deleteAgenda(${a.id})">Excluir</button>
      </div>
    `;
  }).join('');
}

async function loadAgenda() {
  try {
    const r = await authFetch('/api/agenda');
    AGENDA = await r.json();
  } catch (e) {
    AGENDA = [];
  }
}

function editAgenda(id) {
  const a = AGENDA.find(x => x.id === id);
  if (!a) return;
  AGENDA_EDIT_ID = id;
  renderTab();
  setTimeout(() => {
    document.getElementById('ag-opp').value = a.opponent;
    document.getElementById('ag-date').value = a.match_date;
    document.getElementById('ag-time').value = a.match_time || '';
    document.getElementById('ag-type').value = a.match_type || 'liga';
    document.getElementById('ag-loc').value = a.location || '';
    document.getElementById('ag-notes').value = a.notes || '';
    window.scrollTo({top: 0, behavior: 'smooth'});
  }, 50);
}

function cancelAgendaEdit() {
  AGENDA_EDIT_ID = null;
  if (!isAdmin()) {
    document.querySelectorAll('.tab').forEach(el => {
      if (['JOGADORES','COMPARAR','CONFRONTOS','ADVERSÁRIOS'].includes((el.textContent || '').trim())) el.remove();
    });
  }
  renderTab();
}

async function saveAgenda(ev) {
  ev.preventDefault();
  const body = {
    opponent: document.getElementById('ag-opp').value.trim(),
    match_date: document.getElementById('ag-date').value,
    match_time: document.getElementById('ag-time').value || null,
    match_type: document.getElementById('ag-type').value,
    location: document.getElementById('ag-loc').value.trim() || null,
    notes: document.getElementById('ag-notes').value.trim() || null,
  };
  if (!body.opponent || !body.match_date) return;
  try {
    const url = AGENDA_EDIT_ID ? `/api/agenda/${AGENDA_EDIT_ID}` : '/api/agenda';
    const method = AGENDA_EDIT_ID ? 'PUT' : 'POST';
    const r = await authFetch(url, {
      method,
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(body)
    });
    if (!r.ok) throw new Error('Erro ao salvar');
    AGENDA_EDIT_ID = null;
    await loadAgenda();
    renderTab();
  } catch (e) {
    alert('Erro: ' + e.message);
  }
}

async function deleteAgenda(id) {
  if (!confirm('Excluir este agendamento?')) return;
  try {
    await authFetch('/api/agenda/' + id, {method: 'DELETE'});
    await loadAgenda();
    renderTab();
  } catch (e) {
    alert('Erro: ' + e.message);
  }
}

function showMatchDetails(matchId) {
  const m = DATA.matches.find(x => String(x.match_id) === String(matchId));
  if (!m) return;
  const players = [...(m.players_ratings || [])].sort((a,b) => Number(b.sofi_rating || b.rating || 0) - Number(a.sofi_rating || a.rating || 0));
  const positives = [];
  const negatives = [];
  if (m.result === 'V') positives.push('Resultado positivo e eficiência para vencer o confronto.');
  if (m.goals_for >= 3) positives.push(`Bom volume ofensivo: ${m.goals_for} gols marcados.`);
  if (m.goals_against === 0) positives.push('Clean sheet coletivo: defesa não sofreu gols.');
  if (players.some(p => Number(p.sofi_rating || 0) >= 8)) positives.push('Houve destaque individual com nota Sofi acima de 8.');
  if (m.result === 'D') negatives.push('Resultado negativo: revisar tomada de decisão e transições.');
  if (m.goals_against >= 3) negatives.push(`Atenção defensiva: ${m.goals_against} gols sofridos.`);
  if (players.some(p => Number(p.red || 0) > 0)) negatives.push('Cartão vermelho impactou o desempenho coletivo.');
  if (players.filter(p => Number(p.rating || 0) < 6).length) negatives.push('Jogadores com nota EA abaixo de 6 indicam oscilação individual.');
  if (!positives.length) positives.push('Partida equilibrada, sem ponto positivo dominante nos dados disponíveis.');
  if (!negatives.length) negatives.push('Sem alerta grave nos dados disponíveis.');

  let html = `
    <h2>VS ${String(m.opponent || '').toUpperCase()}</h2>
    <p><strong>Resultado:</strong> ${m.result === 'V' ? 'Vitória' : m.result === 'E' ? 'Empate' : 'Derrota'} (${m.score})</p>
    <p><strong>Data:</strong> ${m.date} · <strong>Tipo:</strong> ${m.match_type} · <strong>ID:</strong> ${m.match_id}</p>
    <div class="analytics-cards" style="margin:14px 0;">
      <div class="analytics-card"><div class="v">${m.goals_for}</div><div class="l">Gols Pró</div></div>
      <div class="analytics-card"><div class="v">${m.goals_against}</div><div class="l">Gols Contra</div></div>
      <div class="analytics-card"><div class="v">${players.length}</div><div class="l">Jogadores</div></div>
      <div class="analytics-card"><div class="v">${m.mom_rating || '-'}</div><div class="l">Nota MOM</div></div>
    </div>
    <h3>Resumo</h3>
    <p><strong>MOM:</strong> ${m.mom || 'N/A'}${m.mom_rating ? ' · ' + m.mom_rating : ''}</p>
    <h3>Pontos positivos</h3><ul>${positives.map(x => `<li>${x}</li>`).join('')}</ul>
    <h3>Pontos negativos</h3><ul>${negatives.map(x => `<li>${x}</li>`).join('')}</ul>
    <h3>Notas dos jogadores</h3>
    <table class="history-table">
      <thead><tr><th>#</th><th>Jogador</th><th>Pos</th><th>Sofi</th><th>EA</th><th>G</th><th>A</th><th>Chu</th><th>Pass%</th><th>Des%</th><th>Des</th><th>Def</th><th>SG</th><th>Verm</th><th>MOM</th></tr></thead>
      <tbody>
        ${players.map((p, idx) => `<tr>
          <td>${idx + 1}</td><td><strong>${p.name}</strong></td><td>${p.pos || '-'}</td><td><span class="sofi">${p.sofi_rating ?? p.rating}</span></td><td>${p.rating ?? '-'}</td>
          <td>${p.goals || 0}</td><td>${p.assists || 0}</td><td>${p.shots || 0}</td><td>${p.pass_pct || 0}%</td><td>${p.tackle_pct || 0}%</td><td>${p.tackles_made || 0}</td>
          <td>${p.saves || 0}</td><td>${p.clean_sheet || 0}</td><td>${p.red || 0}</td><td>${p.mom ? 'Sim' : ''}</td>
        </tr>`).join('')}
      </tbody>
    </table>
  `;
  document.getElementById('modalContent').innerHTML = html;
  document.getElementById('modal').classList.add('active');
}

async function analyzePlayer(name) {
  document.getElementById('modalContent').innerHTML = '<div class="loading"><div class="spinner"></div> Analisando jogador com IA...</div>';
  document.getElementById('modal').classList.add('active');
  
  try {
    const r = await authFetch(`/api/ai/player?player_name=${encodeURIComponent(name)}&match_type=${encodeURIComponent(CURRENT_MATCH_TYPE)}`, { method: 'POST' });
    const data = await r.json();
    document.getElementById('modalContent').innerHTML = renderMarkdown(data.analysis);
  } catch (e) {
    document.getElementById('modalContent').innerHTML = `<p style="color:var(--red);">Erro: ${e.message}</p>`;
  }
}

async function showPlayerDetail(name) {
  const mc = document.getElementById('modalContent');
  mc.innerHTML = '<div class="loading"><div class="spinner"></div> Carregando analytics...</div>';
  document.getElementById('modal').classList.add('active');
  try {
    const r = await authFetch('/api/player/' + encodeURIComponent(name) + '/analytics?match_type=' + encodeURIComponent(CURRENT_MATCH_TYPE));
    if (!r.ok) throw new Error('Não encontrado ou sem dados suficientes');
    const data = await r.json();
    mc.innerHTML = renderPlayerDetailHTML(data);
    setTimeout(() => renderPlayerCharts(data), 80);
  } catch (e) {
    mc.innerHTML = `<p style="color:var(--red);">Erro: ${e.message}</p>`;
  }
}

function heatColor(v) {
  const alpha = 0.08 + Math.min(0.82, Number(v || 0) * 0.82);
  return `rgba(0,255,115,${alpha})`;
}

function renderHeatmap(heatmap) {
  const z = (heatmap && heatmap.zones) || {};
  const labels = [
    ['att_left','Ataque E'], ['att_center','Ataque C'], ['att_right','Ataque D'],
    ['mid_left','Meio E'], ['mid_center','Meio C'], ['mid_right','Meio D'],
    ['def_left','Defesa E'], ['def_center','Defesa C'], ['def_right','Defesa D'],
  ];
  return `
    <div class="heatmap-wrap">
      <div class="heatmap-field">
        ${labels.map(([key, label]) => `<div class="heat-zone" style="background:${heatColor(z[key])}">${label}</div>`).join('')}
      </div>
      <div class="mini-insights">
        <div class="mini-insight"><div class="k">Perfil</div><div class="v">${heatmap?.profile || '-'}</div></div>
        <div class="mini-insight"><div class="k">Leitura</div><div class="v">Quanto mais verde, maior a presença estimada naquela zona.</div></div>
        <div class="analytics-note">${heatmap?.disclaimer || 'Mapa estimado por perfil estatístico.'}</div>
      </div>
    </div>`;
}

function matchLine(m) {
  if (!m) return '<div class="mini-insight"><div class="v">Sem dados</div></div>';
  return `<div class="mini-insight"><div class="k">${m.date} · ${m.match_type}</div><div class="v">VS ${m.opponent} · ${m.result} ${m.score} · Sofi ${m.sofi_rating} · EA ${m.rating}</div></div>`;
}

function plainScoutSummary(text) {
  return String(text || '')
    .replace(/#{1,6}\s*/g, '')
    .replace(/\*\*/g, '')
    .replace(/^-\s*/gm, '')
    .split('\n')
    .map(x => x.trim())
    .filter(Boolean)
    .slice(0, 3)
    .join(' ');
}

function renderPlayerDetailHTML(data) {
  const p = data.player;
  const h = data.history || [];
  const avg = data.averages || {};
  const totals = data.totals || {};
  const adv = data.advanced || {};
  const rank = data.ranking || {};
  const cmp = data.team_comparison || {};
  const trend = data.trend || {};
  const safeName = p.name.replace(/'/g, "\\'");
  const radar = adv.radar || {};
  const profile = profileForPlayer(p.name);
  const psBadges = (profile.playstyles || []).map(x => `<span class="tag liga" style="margin-right:6px;">${playstyleIcon(x)} ${x}</span>`).join('');
  const analyzedGames = Number(data.games_with_history ?? h.length ?? 0) || h.length || 0;

  return `
    <div class="player-detail">
      <div class="analytics-hero">
        <div>
          <h2>${p.name} <span class="pos-tag">${p.position}</span></h2>
          <div style="color:var(--text-2);font-size:13px;">${analyzedGames} partidas analisadas neste filtro · ranking ${rank.rating_rank_label || '-'} · tendência ${trend.status || '-'}</div>
          ${psBadges ? `<div style="margin-top:8px;">${psBadges}</div>` : ''}
          <div class="analytics-note" style="margin-top:8px;">${plainScoutSummary(data.scout_report || '').slice(0, 360)}</div>
        </div>
        <div class="analytics-score"><div class="num">${adv.analytic_score || 0}</div><div class="lab">Analítica</div></div>
      </div>

      <div class="analytics-cards">
        <div class="analytics-card"><div class="v">${avg.rating || '-'}</div><div class="l">Média EA</div></div>
        <div class="analytics-card"><div class="v">${avg.sofi_rating || '-'}</div><div class="l">Média Sofi</div></div>
        <div class="analytics-card"><div class="v">${adv.analytic_score || 0}</div><div class="l">Final</div></div>
        <div class="analytics-card"><div class="v">${totals.goals || 0}</div><div class="l">Gols</div></div>
        <div class="analytics-card"><div class="v">${totals.assists || 0}</div><div class="l">Assist</div></div>
        <div class="analytics-card"><div class="v">${(totals.goals || 0) + (totals.assists || 0)}</div><div class="l">G+A</div></div>
        <div class="analytics-card"><div class="v">${avg.goals_per_game || 0}</div><div class="l">G/J</div></div>
        <div class="analytics-card"><div class="v">${avg.assists_per_game || 0}</div><div class="l">A/J</div></div>
        <div class="analytics-card"><div class="v">${totals.shots || 0}</div><div class="l">Chutes</div></div>
        <div class="analytics-card"><div class="v">${avg.shots_per_game || 0}</div><div class="l">Chu/J</div></div>
        <div class="analytics-card"><div class="v">${avg.passes_pct || 0}%</div><div class="l">Pass%</div></div>
        <div class="analytics-card"><div class="v">${totals.tackles || 0}</div><div class="l">Desarmes</div></div>
        <div class="analytics-card"><div class="v">${avg.tackle_pct || 0}%</div><div class="l">Des%</div></div>
        <div class="analytics-card"><div class="v">${avg.tackles_per_game || 0}</div><div class="l">Des/J</div></div>
        <div class="analytics-card"><div class="v">${totals.saves || 0}</div><div class="l">Defesas</div></div>
        <div class="analytics-card"><div class="v">${avg.saves_per_game || 0}</div><div class="l">Def/J</div></div>
        <div class="analytics-card"><div class="v">${totals.clean_sheets || 0}</div><div class="l">SG</div></div>
        <div class="analytics-card"><div class="v">${totals.moms || 0}</div><div class="l">MOMs</div></div>
        <div class="analytics-card"><div class="v">${adv.regularity || 0}%</div><div class="l">Regularidade</div></div>
        <div class="analytics-card"><div class="v">${adv.consistency || 0}</div><div class="l">Consist.</div></div>
        <div class="analytics-card"><div class="v">${adv.offensive_impact || 0}</div><div class="l">Impacto Of.</div></div>
        <div class="analytics-card"><div class="v">${adv.defensive_impact || 0}</div><div class="l">Impacto Def.</div></div>
        <div class="analytics-card"><div class="v">${adv.clutch_score || 0}</div><div class="l">Clutch</div></div>
        <div class="analytics-card"><div class="v">${adv.risk || 0}</div><div class="l">Risco</div></div>
        <div class="analytics-card"><div class="v">${totals.red_cards || 0}</div><div class="l">Vermelhos</div></div>
      </div>

      <div class="analytics-grid">
        <div class="chart-box"><div class="chart-title">Evolução EA</div><canvas id="ratingChart" width="420" height="210"></canvas></div>
        <div class="chart-box"><div class="chart-title">Evolução Sofi</div><canvas id="sofiChart" width="420" height="210"></canvas></div>
        <div class="chart-box"><div class="chart-title">Gols por partida</div><canvas id="goalsChart" width="420" height="210"></canvas></div>
        <div class="chart-box"><div class="chart-title">Assistências por partida</div><canvas id="assistsChart" width="420" height="210"></canvas></div>
        <div class="chart-box wide"><div class="chart-title">Radar técnico</div><canvas id="radarChart" width="860" height="310"></canvas></div>
      </div>

      <div class="section-title">Mapa de Calor Estimado</div>
      ${renderHeatmap(data.heatmap)}

      <div class="section-title">Melhor / Pior / Tendência</div>
      <div class="mini-insights" style="grid-template-columns:repeat(2,1fr);display:grid;">
        ${matchLine(data.best_match)}
        ${matchLine(data.worst_match)}
        <div class="mini-insight"><div class="k">Contra quem mais performou</div><div class="v">${data.best_opponent ? `${data.best_opponent.opponent} · ${data.best_opponent.avg_sofi}` : '-'}</div></div>
        <div class="mini-insight"><div class="k">Contra quem menos performou</div><div class="v">${data.worst_opponent ? `${data.worst_opponent.opponent} · ${data.worst_opponent.avg_sofi}` : '-'}</div></div>
        <div class="mini-insight"><div class="k">Comparação elenco</div><div class="v">Rating vs média: ${cmp.player_vs_team_rating || 0} · G/J vs média: ${cmp.player_vs_team_goals_per_game || 0}</div></div>
        <div class="mini-insight"><div class="k">Avançadas</div><div class="v">Ofensivo ${adv.offensive_impact || 0} · Defensivo ${adv.defensive_impact || 0} · Risco ${adv.risk || 0}</div></div>
      </div>

      <div class="section-title">Relatório Scout Offline</div>
      <div class="analytics-note">${renderMarkdown(data.scout_report || '')}</div>

      <div class="section-title" style="margin-top:18px;">Últimas ${h.length} Partidas</div>
      ${h.length === 0 ? '<div style="color:var(--text-2);padding:20px;text-align:center;">Nenhuma partida com participação registrada.</div>' : `
      <table class="history-table">
        <thead><tr><th>Data</th><th>Tipo</th><th>Adversário</th><th>Resultado</th><th>Pos</th><th>Sofi</th><th>EA</th><th>G</th><th>A</th><th>Chu</th><th>Pass%</th><th>Des%</th><th>Des</th><th>Def</th><th>SG</th><th>Verm</th><th>MOM</th></tr></thead>
        <tbody>
          ${h.map(x => `<tr>
            <td>${x.date}</td><td><span class="tag ${x.match_type}">${x.match_type}</span></td><td>${x.opponent}</td>
            <td><span class="tag ${x.result.toLowerCase()}">${x.result} ${x.score}</span></td><td>${x.position}</td>
            <td><span class="sofi">${x.sofi_rating}</span></td><td>${x.rating}</td><td>${x.goals}</td><td>${x.assists}</td><td>${x.shots}</td><td>${x.pass_pct}%</td><td>${x.tackle_pct}%</td><td>${x.tackles_made || 0}</td><td>${x.saves || 0}</td><td>${x.clean_sheet || 0}</td><td>${x.red || 0}</td><td>${x.mom ? '⭐' : ''}</td>
          </tr>`).join('')}
        </tbody>
      </table>`}

      <div style="margin-top:18px;display:flex;gap:8px;">
        <button class="btn-primary" style="padding:8px 18px;" onclick="analyzePlayer('${safeName}')">🤖 Analisar com IA</button>
      </div>
    </div>`;
}

function renderPlayerCharts(data) {
  if (!window.Chart) return;
  if (window.PLAYER_CHARTS) { window.PLAYER_CHARTS.forEach(ch => { try { ch.destroy(); } catch(e) {} }); }
  window.PLAYER_CHARTS = [];
  const s = data.series || [];
  const labels = s.map(x => (x.date || '').slice(0,5) + ' ' + (x.opponent || '').slice(0,8));
  const grid = 'rgba(255,255,255,0.08)';
  const ticks = '#888';
  const green = '#00FF73';
  const yellow = '#ffaa00';
  const red = '#ff3344';
  const common = {responsive:false, maintainAspectRatio:false, animation:false, plugins:{legend:{display:false}}, scales:{x:{ticks:{color:ticks, maxRotation:45, minRotation:0}, grid:{color:grid}}, y:{ticks:{color:ticks}, grid:{color:grid}}}};
  const make = (id, type, values, color, extra={}) => {
    const el = document.getElementById(id);
    if (!el) return;
    const chart = new Chart(el, {type, data:{labels, datasets:[{data:values, borderColor:color, backgroundColor: color + '66', tension:0.35, fill:type==='line'?false:true}]}, options:{...common, ...extra}});
    window.PLAYER_CHARTS.push(chart);
  };
  make('ratingChart', 'line', s.map(x => x.rating || 0), green, {scales:{...common.scales, y:{min:0,max:10,ticks:{color:ticks},grid:{color:grid}}}});
  make('sofiChart', 'line', s.map(x => x.sofi_rating || 0), yellow, {scales:{...common.scales, y:{min:0,max:10,ticks:{color:ticks},grid:{color:grid}}}});
  make('goalsChart', 'bar', s.map(x => x.goals || 0), green);
  make('assistsChart', 'bar', s.map(x => x.assists || 0), yellow);
  const radar = data.advanced?.radar || {};
  const radarEl = document.getElementById('radarChart');
  if (radarEl) { const radarChart = new Chart(radarEl, {type:'radar', data:{labels:Object.keys(radar), datasets:[{data:Object.values(radar), borderColor:green, backgroundColor:'rgba(0,255,115,0.22)', pointBackgroundColor:green}]}, options:{responsive:false, maintainAspectRatio:false, animation:false, scales:{r:{min:0,max:100, ticks:{display:false}, grid:{color:grid}, angleLines:{color:grid}, pointLabels:{color:ticks}}}, plugins:{legend:{display:false}}}}); window.PLAYER_CHARTS.push(radarChart); }
}
function generateTeamAnalysisClient(team) {
  const nl = String.fromCharCode(10);
  const players = team.players || [];
  const lines = players.map(p => `- **${p.role}** - ${p.name} (EA ${p.rating}, ${p.position}, encaixe ${p.fit})`).join(nl);
  const byRole = {GK:0, DEF:0, MID:0, FWD:0};
  players.forEach(p => { byRole[p.family] = (byRole[p.family] || 0) + 1; });
  const avg = players.length ? (players.reduce((sum,p) => sum + Number(p.rating || 0), 0) / players.length).toFixed(2) : '-';
  const improvised = players.filter(p => p.fit === 'improvisado');
  const adapted = players.filter(p => p.fit === 'adaptado');
  const missing = team.missing_slots || [];
  return [
    `## Time Ideal - Formação ${team.formation}`,
    '',
    '### Escalação',
    lines || '- Nenhum jogador disponível no filtro atual.',
    '',
    '### Leitura do elenco',
    `A escalação acima usa exatamente o time que está no campinho agora, com estatísticas somente do clube/filtro atual e respeitando posições manuais salvas no Cadastro. Média EA do XI: **${avg}**.`,
    '',
    '### Distribuição',
    `- Goleiros: ${byRole.GK || 0}`,
    `- Defensores: ${byRole.DEF || 0}`,
    `- Meio-campistas: ${byRole.MID || 0}`,
    `- Atacantes: ${byRole.FWD || 0}`,
    '',
    '### Pontos fortes',
    '- Escolha baseada em nota média, encaixe por função e desempenho no clube pesquisado.',
    '- Jogadores naturais foram priorizados nas posições mais sensíveis, especialmente GK e zaga.',
    '- Ajustes manuais têm prioridade sobre a posição favorita da EA.',
    '',
    '### Alertas',
    missing.length ? '- Faltou jogador compatível para: ' + missing.join(', ') : '- Nenhuma posição ficou sem jogador compatível.',
    adapted.length ? '- Adaptados: ' + adapted.map(p => `${p.name} em ${p.role}`).join(', ') : '- Sem adaptações relevantes.',
    improvised.length ? '- Improvisados: ' + improvised.map(p => `${p.name} em ${p.role}`).join(', ') : '- Sem improvisos críticos.',
    '',
    '### Recomendação prática',
    'Use essa formação se quiser preservar encaixe e nota média. Se algum jogador aparecer fora da função real, corrija na aba **Cadastro**; essa correção passa a valer no campinho e nesta análise.'
  ].join(nl);
}

async function analyzeTeam() {
  document.getElementById('modalContent').innerHTML = '<div class="loading"><div class="spinner"></div> Gerando análise do time ideal...</div>';
  document.getElementById('modal').classList.add('active');
  try {
    const team = buildIdealTeamClient(IDEAL_FORMATION);
    document.getElementById('modalContent').innerHTML = renderMarkdown(generateTeamAnalysisClient(team));
  } catch (e) {
    document.getElementById('modalContent').innerHTML = `<p style="color:var(--red);">Erro: ${e.message}</p>`;
  }
}

function renderMarkdown(md) {
  if (!md) return '';
  let html = String(md);
  if (html.includes('<h2>') || html.includes('<h3>') || html.includes('<p>') || html.includes('<ul>')) {
    return html
      .replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>')
      .replace(/\n/g, '<br>');
  }
  html = html
    .replace(/^### (.+)$/gm, '<h3>$1</h3>')
    .replace(/^## (.+)$/gm, '<h2>$1</h2>')
    .replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>')
    .replace(/^- (.+)$/gm, '<li>$1</li>')
    .replace(/(<li>.*?<\/li>)/gs, '<ul>$1</ul>')
    .replace(/\n\n/g, '</p><p>')
    .replace(/^([^<\n].+)$/gm, '<p>$1</p>')
    .replace(/<p><\/p>/g, '');
  return html;
}

function closeModal() {
  document.getElementById('modal').classList.remove('active');
}

async function importLocalHistoryToServer() {
  try {
    if (!DATA || !DATA.club) return;
    const localMatches = loadLocalMatchHistory(DATA.club);
    const merged = mergeMatchHistoryForClub(DATA.club, localMatches, DATA.matches || []);
    if (!merged.length) return;
    await authFetch('/api/import-history', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({club: DATA.club, matches: merged, players: DATA.players || []})
    });
  } catch (e) {
    console.warn('Nao conseguiu importar historico local antes da sync', e);
  }
}
async function startSync() {
  if (!isAdmin()) { alert('Apenas administradores podem sincronizar o clube.'); return; }
  const progress = document.getElementById('syncProgress');
  const log = document.getElementById('syncLog');
  const fill = document.getElementById('syncFill');
  const stepEl = document.getElementById('syncStep');
  
  progress.classList.add('active');
  log.innerHTML = '';
  fill.style.width = '0%';
  
  if (DATA && DATA.club && DATA.matches) saveLocalMatchHistory(DATA.club, DATA.matches);
  await importLocalHistoryToServer();
  const clubName = (document.getElementById('clubInput')?.value || 'DESAGREGADOS SC').trim();
  const evt = new EventSource('/api/sync-stream?club_name=' + encodeURIComponent(clubName) + '&access_token=' + encodeURIComponent(AUTH_TOKEN));
  
  evt.onmessage = (e) => {
    try {
      const data = JSON.parse(e.data);
      
      if (data.msg) {
        log.innerHTML += `<div class="sync-log-line">${data.msg}</div>`;
        log.scrollTop = log.scrollHeight;
      }
      
      if (data.step && data.total) {
        const pct = (data.step / data.total) * 100;
        fill.style.width = pct + '%';
        stepEl.textContent = `Etapa ${data.step} de ${data.total}`;
      }
      
      if (data.done) {
        evt.close();
        if (data.success) {
          stepEl.textContent = '✅ Concluído!';
          fill.style.width = '100%';
          setTimeout(async () => {
            progress.classList.remove('active');
            const profileBackup = {...(PLAYER_PROFILES || {})};
            try { saveLocalPlayerProfiles(); } catch (e) {}
            await loadData();
            protectDataMatchHistory();
            PLAYER_PROFILES = {...(DATA?.player_profiles || {}), ...profileBackup, ...loadLocalPlayerProfiles()};
            if (DATA) DATA.player_profiles = {...PLAYER_PROFILES};
            saveLocalPlayerProfiles();
            render();
          }, 1500);
        } else if (data.error) {
          stepEl.textContent = `❌ Erro: ${data.error}`;
        }
      }
    } catch (err) {
      console.error(err);
    }
  };
  
  evt.onerror = () => {
    evt.close();
    stepEl.textContent = '❌ Conexão perdida';
  };
}

document.addEventListener('keydown', e => {
  if (e.key === 'Escape') closeModal();
});

initAuth();
</script>
</body>
</html>"""


# ============================================================
# MAIN
# ============================================================

if __name__ == "__main__":
    import uvicorn
    print("\n" + "="*60)
    print(f"  {APP_NAME}")
    print("="*60)
    print(f"  🌐 Acesse:  http://localhost:8000")
    print(f"  📚 Docs:    http://localhost:8000/docs")
    print(f"  💾 Cache:   {JSON_CACHE}")
    print(f"  🗄️  Banco:   {DB_FILE}")
    print("="*60 + "\n")
    
    uvicorn.run(app, host="0.0.0.0", port=8000)










































































