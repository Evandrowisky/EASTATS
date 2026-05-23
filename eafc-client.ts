/**
 * EA FC Clubs API Client
 * Integrates with the official EA FC API to fetch club data, player stats, and match history
 */

import { invokeLLM } from "./_core/llm";
import { upsertClub, upsertPlayer, upsertMatch, createAIReport } from "./db";
import type { InsertClub, InsertPlayer, InsertMatch, InsertAIReport } from "../drizzle/schema";

const BASE_URL = "https://proclubs.ea.com/api/fc";

const HEADERS = {
  accept: "application/json,text/plain,*/*",
  "accept-language": "pt-BR,pt;q=0.9,en-US;q=0.8,en;q=0.7",
  origin: "https://www.ea.com",
  referer: "https://www.ea.com/",
  "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
};

interface EAFCResponse<T = unknown> {
  success: boolean;
  statusCode: number;
  data?: T;
  error?: string;
}

async function fetchEAFC<T>(endpoint: string, params: Record<string, string>): Promise<EAFCResponse<T>> {
  try {
    const url = new URL(`${BASE_URL}/${endpoint}`);
    Object.entries(params).forEach(([key, value]) => {
      url.searchParams.append(key, value);
    });

    const controller = new AbortController();
    const timeoutId = setTimeout(() => controller.abort(), 30000);
    
    const response = await fetch(url.toString(), {
      headers: HEADERS,
      signal: controller.signal,
    });
    
    clearTimeout(timeoutId);

    const data = await response.json();

    return {
      success: response.ok,
      statusCode: response.status,
      data: data as T,
    };
  } catch (error) {
    return {
      success: false,
      statusCode: 0,
      error: error instanceof Error ? error.message : "Unknown error",
    };
  }
}

export async function searchClub(clubName: string, platform: string = "common-gen5") {
  return fetchEAFC("allTimeLeaderboard/search", {
    clubName,
    platform,
  });
}

export async function getClubInfo(clubId: string, platform: string = "common-gen5") {
  return fetchEAFC("clubs/info", {
    clubIds: clubId,
    platform,
  });
}

export async function getClubStats(clubId: string, platform: string = "common-gen5") {
  return fetchEAFC("clubs/overallStats", {
    clubIds: clubId,
    platform,
  });
}

export async function getPlayerStats(clubId: string, platform: string = "common-gen5") {
  return fetchEAFC("members/stats", {
    clubId,
    platform,
  });
}

export async function getPlayerCareerStats(clubId: string, platform: string = "common-gen5") {
  return fetchEAFC("members/career/stats", {
    clubId,
    platform,
  });
}

export async function getMatches(clubId: string, matchType: string, platform: string = "common-gen5") {
  return fetchEAFC("clubs/matches", {
    clubIds: clubId,
    platform,
    matchType,
  });
}

export async function syncClubData(clubName: string, platform: string = "common-gen5") {
  try {
    // Search for club
    const searchResult = await searchClub(clubName, platform);
    if (!searchResult.success || !searchResult.data) {
      return { success: false, error: "Club not found" };
    }

    const clubData = Array.isArray(searchResult.data) ? searchResult.data[0] : searchResult.data;
    const clubId = clubData?.clubId || clubData?.club_id;

    if (!clubId) {
      return { success: false, error: "Could not extract club ID" };
    }

    // Fetch all club data
    const [infoResult, statsResult, playerStatsResult, playerCareerResult, matchesLeagueResult, matchesPlayoffResult] = await Promise.all([
      getClubInfo(clubId, platform),
      getClubStats(clubId, platform),
      getPlayerStats(clubId, platform),
      getPlayerCareerStats(clubId, platform),
      getMatches(clubId, "leagueMatch", platform),
      getMatches(clubId, "playoffMatch", platform),
    ]);

    // Save club data
    if (searchResult.data) {
      const clubInfo = Array.isArray(searchResult.data) ? searchResult.data[0] : searchResult.data;
      const club: InsertClub = {
        clubId: String(clubId),
        name: clubInfo?.clubInfo?.name || clubName,
        platform,
        gamesPlayed: parseInt(clubInfo?.gamesPlayed || 0),
        wins: parseInt(clubInfo?.wins || 0),
        ties: parseInt(clubInfo?.ties || 0),
        losses: parseInt(clubInfo?.losses || 0),
        goals: parseInt(clubInfo?.goals || 0),
        goalsAgainst: parseInt(clubInfo?.goalsAgainst || 0),
        cleanSheets: parseInt(clubInfo?.cleanSheets || 0),
        points: parseInt(clubInfo?.points || 0),
        bestDivision: clubInfo?.bestDivision || "",
        stadium: clubInfo?.clubInfo?.customKit?.stadName || "",
        rawJson: JSON.stringify(clubInfo),
      };
      await upsertClub(club);
    }

    // Save player data
    const playerDataSources = [playerStatsResult.data, playerCareerResult.data].filter(Boolean);
    for (const playerData of playerDataSources) {
      if (Array.isArray(playerData)) {
        for (const p of playerData) {
          const player: InsertPlayer = {
            clubId: String(clubId),
            playerName: p?.playerName || p?.name || "Unknown",
            position: p?.position || p?.pos || "",
            games: parseInt(p?.gamesPlayed || p?.games || 0),
            rating: parseInt(p?.ratingAve || p?.rating || 0),
            goals: parseInt(p?.goals || 0),
            assists: parseInt(p?.assists || 0),
            passPercent: parseInt(p?.passSuccessRate || 0),
            duelsPercent: parseInt(p?.tackleSuccessRate || 0),
            shots: parseInt(p?.shots || 0),
            saves: parseInt(p?.saves || 0),
            motm: parseInt(p?.manOfTheMatch || 0),
            rawJson: JSON.stringify(p),
          };
          await upsertPlayer(player);
        }
      }
    }

    // Save match data
    const matchDataSources = [
      { data: matchesLeagueResult.data, type: "leagueMatch" },
      { data: matchesPlayoffResult.data, type: "playoffMatch" },
    ];

    for (const { data: matchData, type } of matchDataSources) {
      if (Array.isArray(matchData)) {
        for (const m of matchData) {
          const goalsFor = parseInt(m?.goals || m?.goalsFor || 0);
          const goalsAgainst = parseInt(m?.goalsAgainst || 0);
          const result = goalsFor > goalsAgainst ? "V" : goalsFor === goalsAgainst ? "E" : "D";

          const match: InsertMatch = {
            clubId: String(clubId),
            matchId: m?.matchId || m?.id || String(Date.now()),
            opponentName: m?.opponentClubName || m?.awayClubName || m?.homeClubName || "Unknown",
            goalsFor,
            goalsAgainst,
            result,
            matchType: type,
            playedAt: m?.timestamp || m?.date || "",
            rawJson: JSON.stringify(m),
          };
          await upsertMatch(match);
        }
      }
    }

    return {
      success: true,
      clubId,
      stats: {
        playersSync: playerDataSources.reduce((sum: number, data) => sum + (Array.isArray(data) ? data.length : 0), 0),
        matchesSync: matchDataSources.reduce((sum: number, { data }) => sum + (Array.isArray(data) ? data.length : 0), 0),
      },
    };
  } catch (error) {
    return {
      success: false,
      error: error instanceof Error ? error.message : "Sync failed",
    };
  }
}

export async function analyzePlayerWithAI(clubId: string, playerName: string, playerStats: Record<string, unknown>, clubStats: Record<string, unknown>, recentMatches: unknown[]) {
  const prompt = `
Analyze the following EA FC player and provide a detailed performance report.

Player: ${playerName}
Player Stats: ${JSON.stringify(playerStats, null, 2)}

Club Stats: ${JSON.stringify(clubStats, null, 2)}

Recent Matches: ${JSON.stringify(recentMatches, null, 2)}

Please provide:
1. Player Summary
2. Strengths
3. Weaknesses
4. Areas for Improvement
5. Recommended Role (Starter/Substitute)
6. Confidence Score (0-100)

Format the response in Portuguese, with clear sections.
`;

  const response = await invokeLLM({
    messages: [
      {
        role: "system",
        content: "You are a professional EA FC Clubs performance analyst. Provide detailed, actionable player analysis.",
      },
      {
        role: "user",
        content: prompt,
      },
    ],
  });

  const analysisContent = response.choices[0]?.message.content;
  const analysis = typeof analysisContent === 'string' ? analysisContent : "Unable to generate analysis";

  const report: InsertAIReport = {
    clubId,
    reportType: "player_analysis",
    playerName,
    prompt,
    response: analysis,
  };

  await createAIReport(report);

  return analysis;
}

export async function generateIdealTeamWithAI(clubId: string, players: unknown[], clubStats: Record<string, unknown>, recentMatches: unknown[]) {
  const prompt = `
You are an EA FC Clubs tactical coach. Based on the following data, suggest an ideal team formation and lineup.

Club Stats: ${JSON.stringify(clubStats, null, 2)}

Available Players: ${JSON.stringify(players, null, 2)}

Recent Matches: ${JSON.stringify(recentMatches, null, 2)}

Please provide:
1. Suggested Formation
2. Ideal Starting XI (with positions)
3. Justification for each position
4. Players to Test in Different Positions
5. Squad Weaknesses
6. Tactical Recommendations for Next Matches

Format the response in Portuguese, with clear sections and tactical insights.
`;

  const response = await invokeLLM({
    messages: [
      {
        role: "system",
        content: "You are a professional EA FC Clubs tactical analyst and coach. Provide detailed tactical analysis and team formation recommendations.",
      },
      {
        role: "user",
        content: prompt,
      },
    ],
  });

  const analysisContent = response.choices[0]?.message.content;
  const analysis = typeof analysisContent === 'string' ? analysisContent : "Unable to generate team analysis";

  const report: InsertAIReport = {
    clubId,
    reportType: "team_ideal",
    playerName: "",
    prompt,
    response: analysis,
  };

  await createAIReport(report);

  return analysis;
}
