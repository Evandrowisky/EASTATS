"""
Scout Clubs IA Pro - Backend API (FastAPI)
Integração com EA FC API e análise por IA
"""

import os
import json
import sqlite3
from datetime import datetime
from typing import Any, Dict, List, Optional

from dotenv import load_dotenv
from fastapi import FastAPI, Query
from fastapi.responses import JSONResponse
from curl_cffi import requests
from openai import OpenAI


# ============================================================
# CONFIGURAÇÕES
# ============================================================

load_dotenv()

APP_NAME = "Scout Clubs IA Pro"
DB_PATH = "scout_clubs.db"

BASE_URL = "https://proclubs.ea.com/api/fc"

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")

DEFAULT_PLATFORM = "common-gen5"
DEFAULT_CLUB_NAME = "DESAGREGADOS SC"
DEFAULT_CLUB_ID = "3549624"

HEADERS = {
    "accept": "application/json,text/plain,*/*",
    "accept-language": "pt-BR,pt;q=0.9,en-US;q=0.8,en;q=0.7",
    "origin": "https://www.ea.com",
    "referer": "https://www.ea.com/",
    "user-agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
}

app = FastAPI(title=APP_NAME)


# ============================================================
# BANCO DE DADOS
# ============================================================

def conectar():
    """Conecta ao banco de dados SQLite"""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    """Inicializa o banco de dados com as tabelas necessárias"""
    conn = conectar()
    cur = conn.cursor()

    cur.execute("""
        CREATE TABLE IF NOT EXISTS clubs (
            club_id TEXT PRIMARY KEY,
            name TEXT,
            platform TEXT,
            games_played INTEGER,
            wins INTEGER,
            ties INTEGER,
            losses INTEGER,
            goals INTEGER,
            goals_against INTEGER,
            clean_sheets INTEGER,
            points INTEGER,
            best_division TEXT,
            stadium TEXT,
            raw_json TEXT,
            updated_at TEXT
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS players (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            club_id TEXT,
            player_name TEXT,
            position TEXT,
            games INTEGER,
            rating REAL,
            goals INTEGER,
            assists INTEGER,
            pass_pct REAL,
            duels_pct REAL,
            shots INTEGER,
            saves INTEGER,
            motm INTEGER,
            raw_json TEXT,
            updated_at TEXT,
            UNIQUE(club_id, player_name)
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS matches (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            club_id TEXT,
            match_id TEXT,
            opponent_name TEXT,
            goals_for INTEGER,
            goals_against INTEGER,
            result TEXT,
            match_type TEXT,
            played_at TEXT,
            raw_json TEXT,
            created_at TEXT,
            UNIQUE(club_id, match_id, match_type)
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS ai_reports (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            club_id TEXT,
            report_type TEXT,
            player_name TEXT,
            prompt TEXT,
            response TEXT,
            created_at TEXT
        )
    """)

    conn.commit()
    conn.close()


init_db()


# ============================================================
# HELPERS
# ============================================================

def agora():
    """Retorna timestamp atual formatado"""
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def safe_int(value, default=0):
    """Converte valor para inteiro com segurança"""
    try:
        if value is None or value == "":
            return default
        return int(float(str(value).replace(",", ".")))
    except Exception:
        return default


def safe_float(value, default=0.0):
    """Converte valor para float com segurança"""
    try:
        if value is None or value == "":
            return default
        texto = str(value).replace("%", "").replace(",", ".")
        return float(texto)
    except Exception:
        return default


def pick(d: Dict[str, Any], keys: List[str], default=None):
    """Extrai primeiro valor encontrado de um dicionário"""
    if not isinstance(d, dict):
        return default

    for key in keys:
        if key in d and d[key] not in [None, ""]:
            return d[key]

    lower_map = {str(k).lower(): v for k, v in d.items()}

    for key in keys:
        k = key.lower()
        if k in lower_map and lower_map[k] not in [None, ""]:
            return lower_map[k]

    return default


def json_dump(data):
    """Serializa dados para JSON"""
    return json.dumps(data, ensure_ascii=False, indent=2)


def linhas_para_dict(rows):
    """Converte linhas SQLite para lista de dicionários"""
    return [dict(row) for row in rows]


# ============================================================
# CLIENTE EA CLUBS
# ============================================================

class EAClubsClient:
    """Cliente para comunicação com API EA FC"""
    
    def __init__(self, platform: str):
        self.platform = platform

    def get(self, endpoint: str, params: Dict[str, Any]) -> Dict[str, Any]:
        """Faz requisição GET para API EA FC"""
        url = f"{BASE_URL}/{endpoint}"

        try:
            r = requests.get(
                url,
                params=params,
                headers=HEADERS,
                timeout=30,
                impersonate="chrome120"
            )

            try:
                data = r.json()
            except Exception:
                data = r.text

            return {
                "success": r.status_code == 200,
                "status_code": r.status_code,
                "url": r.url,
                "endpoint": endpoint,
                "params": params,
                "data": data,
            }

        except Exception as e:
            return {
                "success": False,
                "status_code": 0,
                "url": url,
                "endpoint": endpoint,
                "params": params,
                "error": str(e),
                "data": None,
            }

    def search_club(self, club_name: str):
        """Busca clube por nome"""
        return self.get(
            "allTimeLeaderboard/search",
            {
                "clubName": club_name,
                "platform": self.platform,
            }
        )

    def club_info(self, club_id: str):
        """Obtém informações do clube"""
        return self.get(
            "clubs/info",
            {
                "clubIds": club_id,
                "platform": self.platform,
            }
        )

    def overall_stats(self, club_id: str):
        """Obtém estatísticas gerais do clube"""
        return self.get(
            "clubs/overallStats",
            {
                "clubIds": club_id,
                "platform": self.platform,
            }
        )

    def members_career(self, club_id: str):
        """Obtém estatísticas de carreira dos membros"""
        return self.get(
            "members/career/stats",
            {
                "clubId": club_id,
                "platform": self.platform,
            }
        )

    def members_stats(self, club_id: str):
        """Obtém estatísticas dos membros"""
        return self.get(
            "members/stats",
            {
                "clubId": club_id,
                "platform": self.platform,
            }
        )

    def matches(self, club_id: str, match_type: str):
        """Obtém partidas do clube"""
        return self.get(
            "clubs/matches",
            {
                "clubIds": club_id,
                "platform": self.platform,
                "matchType": match_type,
            }
        )


# ============================================================
# CLUBE
# ============================================================

def extrair_club_id_da_busca(response: Dict[str, Any]) -> Optional[str]:
    """Extrai ID do clube da resposta de busca"""
    data = response.get("data")

    if not isinstance(data, list) or not data:
        return None

    first = data[0]
    club_id = first.get("clubId") or first.get("club_id") or first.get("id")

    if club_id:
        return str(club_id)

    return None


def salvar_clube_from_search(response: Dict[str, Any], platform: str) -> Optional[Dict[str, Any]]:
    """Salva clube no banco de dados a partir da resposta de busca"""
    data = response.get("data")

    if not isinstance(data, list) or not data:
        return None

    clube = data[0]
    club_info = clube.get("clubInfo", {}) if isinstance(clube.get("clubInfo"), dict) else {}
    kit = club_info.get("customKit", {}) if isinstance(club_info.get("customKit"), dict) else {}

    club_id = str(clube.get("clubId") or club_info.get("clubId") or DEFAULT_CLUB_ID)
    name = club_info.get("name") or clube.get("clubName") or DEFAULT_CLUB_NAME

    payload = {
        "club_id": club_id,
        "name": name,
        "platform": platform,
        "games_played": safe_int(clube.get("gamesPlayed")),
        "wins": safe_int(clube.get("wins")),
        "ties": safe_int(clube.get("ties")),
        "losses": safe_int(clube.get("losses")),
        "goals": safe_int(clube.get("goals")),
        "goals_against": safe_int(clube.get("goalsAgainst")),
        "clean_sheets": safe_int(clube.get("cleanSheets")),
        "points": safe_int(clube.get("points")),
        "best_division": str(clube.get("bestDivision", "")),
        "stadium": kit.get("stadName", ""),
        "raw_json": json_dump(clube),
        "updated_at": agora(),
    }

    conn = conectar()
    cur = conn.cursor()

    cur.execute("""
        INSERT INTO clubs (
            club_id, name, platform, games_played, wins, ties, losses,
            goals, goals_against, clean_sheets, points, best_division,
            stadium, raw_json, updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(club_id) DO UPDATE SET
            name=excluded.name,
            platform=excluded.platform,
            games_played=excluded.games_played,
            wins=excluded.wins,
            ties=excluded.ties,
            losses=excluded.losses,
            goals=excluded.goals,
            goals_against=excluded.goals_against,
            clean_sheets=excluded.clean_sheets,
            points=excluded.points,
            best_division=excluded.best_division,
            stadium=excluded.stadium,
            raw_json=excluded.raw_json,
            updated_at=excluded.updated_at
    """, (
        payload["club_id"],
        payload["name"],
        payload["platform"],
        payload["games_played"],
        payload["wins"],
        payload["ties"],
        payload["losses"],
        payload["goals"],
        payload["goals_against"],
        payload["clean_sheets"],
        payload["points"],
        payload["best_division"],
        payload["stadium"],
        payload["raw_json"],
        payload["updated_at"],
    ))

    conn.commit()
    conn.close()

    return payload


# ============================================================
# JOGADORES
# ============================================================

def parece_jogador(obj: Dict[str, Any]) -> bool:
    """Verifica se um objeto parece ser um jogador"""
    if not isinstance(obj, dict):
        return False

    chaves_nome = [
        "player_name", "playerName", "name", "personaName", "proName",
        "gamertag", "userName", "displayName", "playername"
    ]

    chaves_stats = [
        "ratingAve", "averageRating", "avgRating", "rating",
        "gamesPlayed", "games", "goals", "assists",
        "passSuccessRate", "tackleSuccessRate", "manOfTheMatch",
        "shots", "saves"
    ]

    tem_nome = any(k in obj for k in chaves_nome)
    tem_stats = any(k in obj for k in chaves_stats)

    return tem_nome or tem_stats


def extrair_lista_de_jogadores(response: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Extrai lista de jogadores da resposta da API"""
    data = response.get("data")
    jogadores = []

    def varrer(obj, chave_pai=None):
        if isinstance(obj, dict):
            if parece_jogador(obj):
                temp = dict(obj)

                if not any(k in temp for k in [
                    "player_name", "playerName", "name", "personaName",
                    "proName", "gamertag", "userName", "displayName", "playername"
                ]):
                    if chave_pai:
                        temp["player_name"] = str(chave_pai)

                jogadores.append(temp)

            for k, v in obj.items():
                if isinstance(v, (dict, list)):
                    varrer(v, k)

        elif isinstance(obj, list):
            for item in obj:
                varrer(item, chave_pai)

    varrer(data)

    jogadores_unicos = {}

    for jogador in jogadores:
        nome = (
            jogador.get("player_name")
            or jogador.get("playerName")
            or jogador.get("name")
            or jogador.get("personaName")
            or jogador.get("proName")
            or jogador.get("gamertag")
            or jogador.get("userName")
            or jogador.get("displayName")
            or jogador.get("playername")
            or ""
        )
        nome = str(nome).strip()

        if not nome:
            nome = f"Jogador_{len(jogadores_unicos) + 1}"

        jogadores_unicos[nome] = jogador

    return list(jogadores_unicos.values())


def normalizar_jogador(raw: Dict[str, Any]) -> Dict[str, Any]:
    """Normaliza dados brutos de jogador"""
    nome = pick(raw, [
        "player_name", "playerName", "name", "personaName", "proName",
        "gamertag", "userName", "displayName", "playername"
    ], "Jogador sem nome")

    position = pick(raw, [
        "position", "pos", "favoritePosition", "proPos", "positionName",
        "class", "role"
    ], "")

    rating = pick(raw, [
        "ratingAve", "averageRating", "avgRating", "rating", "overallRating",
        "overall", "nota", "mediaNota", "avg_match_rating"
    ], 0)

    games = pick(raw, [
        "gamesPlayed", "games", "matches", "appearances", "jogos"
    ], 0)

    goals = pick(raw, ["goals", "goal", "gols"], 0)
    assists = pick(raw, ["assists", "assist", "assistencias"], 0)
    motm = pick(raw, ["manOfTheMatch", "motm", "mom", "homemPartida", "momCount"], 0)

    pass_pct = pick(raw, [
        "passSuccessRate", "passPercent", "pass_pct", "passesPercent",
        "passesCompletedPercent", "passAccuracy", "passSuccess",
        "passesMade", "passes"
    ], 0)

    duels_pct = pick(raw, [
        "tackleSuccessRate", "tacklesPercent", "duelsPercent",
        "duels_pct", "divididas", "divididasPercent", "tackleSuccess"
    ], 0)

    shots = pick(raw, ["shots", "totalShots", "chutes", "shotsOnTarget"], 0)
    saves = pick(raw, ["saves", "defesas", "gkSaves"], 0)

    return {
        "player_name": str(nome),
        "position": str(position),
        "games": safe_int(games),
        "rating": safe_float(rating),
        "goals": safe_int(goals),
        "assists": safe_int(assists),
        "pass_pct": safe_float(pass_pct),
        "duels_pct": safe_float(duels_pct),
        "shots": safe_int(shots),
        "saves": safe_int(saves),
        "motm": safe_int(motm),
        "raw_json": json_dump(raw),
        "updated_at": agora(),
    }


def salvar_jogadores(club_id: str, responses: List[Dict[str, Any]]) -> int:
    """Salva jogadores no banco de dados"""
    conn = conectar()
    cur = conn.cursor()

    jogadores_por_nome = {}

    for response in responses:
        lista = extrair_lista_de_jogadores(response)

        for raw in lista:
            player = normalizar_jogador(raw)
            nome = player["player_name"]

            if not nome or nome == "Jogador sem nome":
                continue

            if nome not in jogadores_por_nome:
                jogadores_por_nome[nome] = player
            else:
                atual = jogadores_por_nome[nome]

                for key in [
                    "games", "rating", "goals", "assists",
                    "pass_pct", "duels_pct", "shots", "saves", "motm"
                ]:
                    if safe_float(player.get(key)) > safe_float(atual.get(key)):
                        atual[key] = player[key]

                if not atual.get("position") and player.get("position"):
                    atual["position"] = player["position"]

                atual["raw_json"] = player["raw_json"]

    total = 0

    for _, p in jogadores_por_nome.items():
        cur.execute("""
            INSERT INTO players (
                club_id, player_name, position, games, rating, goals, assists,
                pass_pct, duels_pct, shots, saves, motm, raw_json, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(club_id, player_name) DO UPDATE SET
                position=excluded.position,
                games=excluded.games,
                rating=excluded.rating,
                goals=excluded.goals,
                assists=excluded.assists,
                pass_pct=excluded.pass_pct,
                duels_pct=excluded.duels_pct,
                shots=excluded.shots,
                saves=excluded.saves,
                motm=excluded.motm,
                raw_json=excluded.raw_json,
                updated_at=excluded.updated_at
        """, (
            club_id,
            p["player_name"],
            p["position"],
            p["games"],
            p["rating"],
            p["goals"],
            p["assists"],
            p["pass_pct"],
            p["duels_pct"],
            p["shots"],
            p["saves"],
            p["motm"],
            p["raw_json"],
            p["updated_at"],
        ))

        total += 1

    conn.commit()
    conn.close()

    return total


# ============================================================
# PARTIDAS
# ============================================================

def extrair_partidas(response: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Extrai lista de partidas da resposta da API"""
    data = response.get("data")

    if not data:
        return []

    partidas = []

    if isinstance(data, list):
        for item in data:
            if isinstance(item, dict):
                partidas.append(item)

    elif isinstance(data, dict):
        for _, value in data.items():
            if isinstance(value, dict):
                partidas.append(value)

    return partidas


def normalizar_partida(raw: Dict[str, Any], club_id: str, match_type: str) -> Dict[str, Any]:
    """Normaliza dados brutos de partida"""
    match_id = str(
        pick(raw, ["matchId", "match_id", "id", "timestamp"], "")
        or f"{match_type}_{abs(hash(json_dump(raw)))}"
    )

    opponent = pick(raw, [
        "opponentClubName", "opponentName", "awayClubName", "homeClubName",
        "club2Name", "team2Name", "opponent"
    ], "Adversário não identificado")

    goals_for = pick(raw, [
        "goals", "goalsFor", "scoreFor", "homeScore", "clubGoals"
    ], 0)

    goals_against = pick(raw, [
        "goalsAgainst", "scoreAgainst", "awayScore", "opponentGoals"
    ], 0)

    gf = safe_int(goals_for)
    ga = safe_int(goals_against)

    if gf > ga:
        result = "V"
    elif gf == ga:
        result = "E"
    else:
        result = "D"

    played_at = str(pick(raw, [
        "timestamp", "date", "matchDate", "createdAt", "timeAgo"
    ], ""))

    return {
        "match_id": match_id,
        "opponent_name": str(opponent),
        "goals_for": gf,
        "goals_against": ga,
        "result": result,
        "match_type": match_type,
        "played_at": played_at,
        "raw_json": json_dump(raw),
    }


def salvar_partidas(club_id: str, response: Dict[str, Any], match_type: str) -> int:
    """Salva partidas no banco de dados"""
    partidas = extrair_partidas(response)
    conn = conectar()
    cur = conn.cursor()

    total = 0

    for raw in partidas:
        p = normalizar_partida(raw, club_id, match_type)

        cur.execute("""
            INSERT INTO matches (
                club_id, match_id, opponent_name, goals_for, goals_against,
                result, match_type, played_at, raw_json, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(club_id, match_id, match_type) DO UPDATE SET
                opponent_name=excluded.opponent_name,
                goals_for=excluded.goals_for,
                goals_against=excluded.goals_against,
                result=excluded.result,
                played_at=excluded.played_at,
                raw_json=excluded.raw_json
        """, (
            club_id,
            p["match_id"],
            p["opponent_name"],
            p["goals_for"],
            p["goals_against"],
            p["result"],
            p["match_type"],
            p["played_at"],
            p["raw_json"],
            agora(),
        ))

        total += 1

    conn.commit()
    conn.close()

    return total


# ============================================================
# CONSULTAS
# ============================================================

def get_club(club_id: Optional[str] = None):
    """Obtém clube do banco de dados"""
    conn = conectar()
    cur = conn.cursor()

    if club_id:
        cur.execute("SELECT * FROM clubs WHERE club_id = ?", (club_id,))
    else:
        cur.execute("SELECT * FROM clubs ORDER BY updated_at DESC LIMIT 1")

    row = cur.fetchone()
    conn.close()

    return dict(row) if row else None


def get_players(club_id: str):
    """Obtém jogadores do clube"""
    conn = conectar()
    cur = conn.cursor()

    cur.execute("""
        SELECT * FROM players
        WHERE club_id = ?
        ORDER BY rating DESC, goals DESC, assists DESC
    """, (club_id,))

    rows = linhas_para_dict(cur.fetchall())
    conn.close()
    return rows


def get_matches(club_id: str, limit: int = 20):
    """Obtém partidas do clube"""
    conn = conectar()
    cur = conn.cursor()

    cur.execute("""
        SELECT * FROM matches
        WHERE club_id = ?
        ORDER BY id DESC
        LIMIT ?
    """, (club_id, limit))

    rows = linhas_para_dict(cur.fetchall())
    conn.close()
    return rows


def get_dashboard_data(club_id: Optional[str] = None):
    """Obtém dados completos para o dashboard"""
    club = get_club(club_id)

    if not club:
        return {
            "club": None,
            "players": [],
            "matches": [],
            "stats": {},
        }

    players = get_players(club["club_id"])
    matches = get_matches(club["club_id"], 20)

    jogos = safe_int(club.get("games_played"))
    wins = safe_int(club.get("wins"))
    goals = safe_int(club.get("goals"))
    goals_against = safe_int(club.get("goals_against"))

    win_rate = round((wins / jogos) * 100, 1) if jogos else 0
    goals_per_game = round(goals / jogos, 2) if jogos else 0
    goals_against_per_game = round(goals_against / jogos, 2) if jogos else 0

    stats = {
        "win_rate": win_rate,
        "goals_per_game": goals_per_game,
        "goals_against_per_game": goals_against_per_game,
        "saldo": goals - goals_against,
        "total_players": len(players),
    }

    return {
        "club": club,
        "players": players,
        "matches": matches,
        "stats": stats,
    }


# ============================================================
# IA
# ============================================================

def chamar_ia(prompt: str) -> str:
    """Chama API OpenAI para gerar análise"""
    if not OPENAI_API_KEY:
        return "OPENAI_API_KEY não configurada no arquivo .env."

    client = OpenAI(api_key=OPENAI_API_KEY)

    response = client.chat.completions.create(
        model=OPENAI_MODEL,
        messages=[
            {
                "role": "system",
                "content": (
                    "Você é um analista profissional de desempenho de EA FC Clubs. "
                    "Analise atletas, partidas e clubes com linguagem clara, objetiva e útil."
                )
            },
            {
                "role": "user",
                "content": prompt
            }
        ],
        temperature=0.4,
    )

    return response.choices[0].message.content


def salvar_ai_report(club_id: str, report_type: str, player_name: str, prompt: str, response: str):
    """Salva relatório de IA no banco de dados"""
    conn = conectar()
    cur = conn.cursor()

    cur.execute("""
        INSERT INTO ai_reports (
            club_id, report_type, player_name, prompt, response, created_at
        )
        VALUES (?, ?, ?, ?, ?, ?)
    """, (
        club_id,
        report_type,
        player_name,
        prompt,
        response,
        agora()
    ))

    conn.commit()
    conn.close()


# ============================================================
# ROTAS API
# ============================================================

@app.get("/api/sync")
def sync_club(
    club_name: str = Query(DEFAULT_CLUB_NAME),
    platform: str = Query(DEFAULT_PLATFORM),
    club_id_fallback: str = Query(DEFAULT_CLUB_ID)
):
    """Sincroniza clube com API EA FC"""
    client = EAClubsClient(platform)

    busca = client.search_club(club_name)
    club_id = extrair_club_id_da_busca(busca) or club_id_fallback

    clube_salvo = None
    if busca.get("success"):
        clube_salvo = salvar_clube_from_search(busca, platform)

    info = client.club_info(club_id)
    overall = client.overall_stats(club_id)
    members_career = client.members_career(club_id)
    members_stats = client.members_stats(club_id)
    matches_league = client.matches(club_id, "leagueMatch")
    matches_playoff = client.matches(club_id, "playoffMatch")

    total_players = salvar_jogadores(club_id, [members_career, members_stats])
    total_matches_league = salvar_partidas(club_id, matches_league, "leagueMatch")
    total_matches_playoff = salvar_partidas(club_id, matches_playoff, "playoffMatch")

    return {
        "success": True,
        "club_id": club_id,
        "club_saved": clube_salvo,
        "saved": {
            "players": total_players,
            "matches_league": total_matches_league,
            "matches_playoff": total_matches_playoff,
        }
    }


@app.get("/api/dashboard")
def api_dashboard(club_id: Optional[str] = None):
    """Retorna dados do dashboard"""
    return get_dashboard_data(club_id)


@app.get("/api/players")
def api_players(club_id: Optional[str] = None):
    """Retorna lista de jogadores"""
    club = get_club(club_id)
    if not club:
        return []
    return get_players(club["club_id"])


@app.get("/api/matches")
def api_matches(club_id: Optional[str] = None, limit: int = 20):
    """Retorna lista de partidas"""
    club = get_club(club_id)
    if not club:
        return []
    return get_matches(club["club_id"], limit)


@app.post("/api/analyze-player")
def analyze_player(club_id: Optional[str] = None, player_name: str = ""):
    """Analisa jogador com IA"""
    club = get_club(club_id)
    if not club:
        return {"error": "Clube não encontrado"}

    players = get_players(club["club_id"])
    player = next((p for p in players if p["player_name"].lower() == player_name.lower()), None)

    if not player:
        return {"error": "Jogador não encontrado"}

    prompt = f"""
    Analise o seguinte jogador de EA FC Clubs:
    
    Nome: {player['player_name']}
    Posição: {player['position']}
    Nota: {player['rating']}
    Jogos: {player['games']}
    Gols: {player['goals']}
    Assistências: {player['assists']}
    Pass %: {player['pass_pct']}
    Duelos %: {player['duels_pct']}
    MOM: {player['motm']}
    
    Forneça uma análise profissional incluindo:
    1. Pontos fortes
    2. Áreas de melhoria
    3. Comparação com a posição
    4. Recomendações táticas
    """

    response = chamar_ia(prompt)
    salvar_ai_report(club["club_id"], "player_analysis", player_name, prompt, response)

    return {
        "player_name": player_name,
        "analysis": response
    }


@app.post("/api/ideal-team")
def ideal_team(club_id: Optional[str] = None):
    """Gera time ideal com IA"""
    club = get_club(club_id)
    if not club:
        return {"error": "Clube não encontrado"}

    players = get_players(club["club_id"])
    matches = get_matches(club["club_id"], 5)

    top_players = sorted(players, key=lambda p: p["rating"], reverse=True)[:11]

    prompt = f"""
    Com base nos seguintes dados do clube {club['name']}, sugira uma formação tática ideal:
    
    Estatísticas do Clube:
    - Taxa de vitória: {club.get('wins', 0)}/{club.get('games_played', 1)}
    - Gols: {club.get('goals', 0)} | Sofridos: {club.get('goals_against', 0)}
    
    Melhores Jogadores:
    {json.dumps(top_players, ensure_ascii=False, indent=2)}
    
    Últimas Partidas:
    {json.dumps(matches, ensure_ascii=False, indent=2)}
    
    Forneça:
    1. Formação sugerida (ex: 4-3-3)
    2. Jogadores recomendados por posição
    3. Justificativas
    4. Estratégia tática
    """

    response = chamar_ia(prompt)
    salvar_ai_report(club["club_id"], "ideal_team", "", prompt, response)

    return {
        "club_name": club["name"],
        "recommendation": response
    }


@app.get("/health")
def health():
    """Health check endpoint"""
    return {"status": "ok", "app": APP_NAME}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
