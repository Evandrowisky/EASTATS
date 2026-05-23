import { eq } from "drizzle-orm";
import { drizzle } from "drizzle-orm/mysql2";
import { InsertUser, users, clubs, players, matches, aiReports, InsertClub, InsertPlayer, InsertMatch, InsertAIReport } from "../drizzle/schema";
import { ENV } from './_core/env';

import type { Club, Player, Match, AIReport } from "../drizzle/schema";

let _db: ReturnType<typeof drizzle> | null = null;

// Lazily create the drizzle instance so local tooling can run without a DB.
export async function getDb() {
  if (!_db && process.env.DATABASE_URL) {
    try {
      _db = drizzle(process.env.DATABASE_URL);
    } catch (error) {
      console.warn("[Database] Failed to connect:", error);
      _db = null;
    }
  }
  return _db;
}

export async function upsertUser(user: InsertUser): Promise<void> {
  if (!user.openId) {
    throw new Error("User openId is required for upsert");
  }

  const db = await getDb();
  if (!db) {
    console.warn("[Database] Cannot upsert user: database not available");
    return;
  }

  try {
    const values: InsertUser = {
      openId: user.openId,
    };
    const updateSet: Record<string, unknown> = {};

    const textFields = ["name", "email", "loginMethod"] as const;
    type TextField = (typeof textFields)[number];

    const assignNullable = (field: TextField) => {
      const value = user[field];
      if (value === undefined) return;
      const normalized = value ?? null;
      values[field] = normalized;
      updateSet[field] = normalized;
    };

    textFields.forEach(assignNullable);

    if (user.lastSignedIn !== undefined) {
      values.lastSignedIn = user.lastSignedIn;
      updateSet.lastSignedIn = user.lastSignedIn;
    }
    if (user.role !== undefined) {
      values.role = user.role;
      updateSet.role = user.role;
    } else if (user.openId === ENV.ownerOpenId) {
      values.role = 'admin';
      updateSet.role = 'admin';
    }

    if (!values.lastSignedIn) {
      values.lastSignedIn = new Date();
    }

    if (Object.keys(updateSet).length === 0) {
      updateSet.lastSignedIn = new Date();
    }

    await db.insert(users).values(values).onDuplicateKeyUpdate({
      set: updateSet,
    });
  } catch (error) {
    console.error("[Database] Failed to upsert user:", error);
    throw error;
  }
}

export async function getUserByOpenId(openId: string) {
  const db = await getDb();
  if (!db) {
    console.warn("[Database] Cannot get user: database not available");
    return undefined;
  }

  const result = await db.select().from(users).where(eq(users.openId, openId)).limit(1);

  return result.length > 0 ? result[0] : undefined;
}

export async function getClubByClubId(clubId: string) {
  const db = await getDb();
  if (!db) return undefined;
  const result = await db.select().from(clubs).where(eq(clubs.clubId, clubId)).limit(1);
  return result.length > 0 ? result[0] : undefined;
}

export async function upsertClub(club: InsertClub): Promise<void> {
  const db = await getDb();
  if (!db) return;
  await db.insert(clubs).values(club).onDuplicateKeyUpdate({
    set: {
      name: club.name,
      gamesPlayed: club.gamesPlayed,
      wins: club.wins,
      ties: club.ties,
      losses: club.losses,
      goals: club.goals,
      goalsAgainst: club.goalsAgainst,
      cleanSheets: club.cleanSheets,
      points: club.points,
      bestDivision: club.bestDivision,
      stadium: club.stadium,
      rawJson: club.rawJson,
      updatedAt: new Date(),
    },
  });
}

export async function getLatestClub() {
  const db = await getDb();
  if (!db) return undefined;
  const result = await db.select().from(clubs).orderBy(clubs.updatedAt).limit(1);
  return result.length > 0 ? result[0] : undefined;
}

export async function getPlayersByClubId(clubId: string) {
  const db = await getDb();
  if (!db) return [];
  return await db.select().from(players).where(eq(players.clubId, clubId));
}

export async function upsertPlayer(player: InsertPlayer): Promise<void> {
  const db = await getDb();
  if (!db) return;
  await db.insert(players).values(player).onDuplicateKeyUpdate({
    set: {
      position: player.position,
      games: player.games,
      rating: player.rating,
      goals: player.goals,
      assists: player.assists,
      passPercent: player.passPercent,
      duelsPercent: player.duelsPercent,
      shots: player.shots,
      saves: player.saves,
      motm: player.motm,
      rawJson: player.rawJson,
      updatedAt: new Date(),
    },
  });
}

export async function getMatchesByClubId(clubId: string, limit: number = 20) {
  const db = await getDb();
  if (!db) return [];
  return await db.select().from(matches).where(eq(matches.clubId, clubId)).limit(limit);
}

export async function upsertMatch(match: InsertMatch): Promise<void> {
  const db = await getDb();
  if (!db) return;
  await db.insert(matches).values(match).onDuplicateKeyUpdate({
    set: {
      opponentName: match.opponentName,
      goalsFor: match.goalsFor,
      goalsAgainst: match.goalsAgainst,
      result: match.result,
      playedAt: match.playedAt,
      rawJson: match.rawJson,
    },
  });
}

export async function createAIReport(report: InsertAIReport): Promise<void> {
  const db = await getDb();
  if (!db) return;
  await db.insert(aiReports).values(report);
}
