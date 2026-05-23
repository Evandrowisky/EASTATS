import { int, mysqlEnum, mysqlTable, text, timestamp, varchar } from "drizzle-orm/mysql-core";

/**
 * Core user table backing auth flow.
 * Extend this file with additional tables as your product grows.
 * Columns use camelCase to match both database fields and generated types.
 */
export const users = mysqlTable("users", {
  /**
   * Surrogate primary key. Auto-incremented numeric value managed by the database.
   * Use this for relations between tables.
   */
  id: int("id").autoincrement().primaryKey(),
  /** Manus OAuth identifier (openId) returned from the OAuth callback. Unique per user. */
  openId: varchar("openId", { length: 64 }).notNull().unique(),
  name: text("name"),
  email: varchar("email", { length: 320 }),
  loginMethod: varchar("loginMethod", { length: 64 }),
  role: mysqlEnum("role", ["user", "admin"]).default("user").notNull(),
  createdAt: timestamp("createdAt").defaultNow().notNull(),
  updatedAt: timestamp("updatedAt").defaultNow().onUpdateNow().notNull(),
  lastSignedIn: timestamp("lastSignedIn").defaultNow().notNull(),
});

export type User = typeof users.$inferSelect;
export type InsertUser = typeof users.$inferInsert;

export const clubs = mysqlTable("clubs", {
  id: int("id").autoincrement().primaryKey(),
  clubId: varchar("clubId", { length: 64 }).notNull().unique(),
  name: varchar("name", { length: 255 }).notNull(),
  platform: varchar("platform", { length: 64 }).notNull(),
  gamesPlayed: int("gamesPlayed").default(0),
  wins: int("wins").default(0),
  ties: int("ties").default(0),
  losses: int("losses").default(0),
  goals: int("goals").default(0),
  goalsAgainst: int("goalsAgainst").default(0),
  cleanSheets: int("cleanSheets").default(0),
  points: int("points").default(0),
  bestDivision: varchar("bestDivision", { length: 255 }),
  stadium: varchar("stadium", { length: 255 }),
  rawJson: text("rawJson"),
  updatedAt: timestamp("updatedAt").defaultNow().onUpdateNow(),
  createdAt: timestamp("createdAt").defaultNow(),
});

export type Club = typeof clubs.$inferSelect;
export type InsertClub = typeof clubs.$inferInsert;

export const players = mysqlTable("players", {
  id: int("id").autoincrement().primaryKey(),
  clubId: varchar("clubId", { length: 64 }).notNull(),
  playerName: varchar("playerName", { length: 255 }).notNull(),
  position: varchar("position", { length: 64 }),
  games: int("games").default(0),
  rating: int("rating").default(0),
  goals: int("goals").default(0),
  assists: int("assists").default(0),
  passPercent: int("passPercent").default(0),
  duelsPercent: int("duelsPercent").default(0),
  shots: int("shots").default(0),
  saves: int("saves").default(0),
  motm: int("motm").default(0),
  rawJson: text("rawJson"),
  updatedAt: timestamp("updatedAt").defaultNow().onUpdateNow(),
});

export type Player = typeof players.$inferSelect;
export type InsertPlayer = typeof players.$inferInsert;

export const matches = mysqlTable("matches", {
  id: int("id").autoincrement().primaryKey(),
  clubId: varchar("clubId", { length: 64 }).notNull(),
  matchId: varchar("matchId", { length: 255 }).notNull(),
  opponentName: varchar("opponentName", { length: 255 }).notNull(),
  goalsFor: int("goalsFor").default(0),
  goalsAgainst: int("goalsAgainst").default(0),
  result: varchar("result", { length: 10 }),
  matchType: varchar("matchType", { length: 64 }),
  playedAt: varchar("playedAt", { length: 255 }),
  rawJson: text("rawJson"),
  createdAt: timestamp("createdAt").defaultNow(),
});

export type Match = typeof matches.$inferSelect;
export type InsertMatch = typeof matches.$inferInsert;

export const aiReports = mysqlTable("aiReports", {
  id: int("id").autoincrement().primaryKey(),
  clubId: varchar("clubId", { length: 64 }).notNull(),
  reportType: varchar("reportType", { length: 64 }).notNull(),
  playerName: varchar("playerName", { length: 255 }),
  prompt: text("prompt"),
  response: text("response"),
  createdAt: timestamp("createdAt").defaultNow(),
});

export type AIReport = typeof aiReports.$inferSelect;
export type InsertAIReport = typeof aiReports.$inferInsert;