import { COOKIE_NAME } from "@shared/const";
import { getSessionCookieOptions } from "./_core/cookies";
import { systemRouter } from "./_core/systemRouter";
import { publicProcedure, router } from "./_core/trpc";
import { z } from "zod";

export const appRouter = router({
  // if you need to use socket.io, read and register route in server/_core/index.ts, all api should start with '/api/' so that the gateway can route correctly
  system: systemRouter,
  auth: router({
    me: publicProcedure.query(opts => opts.ctx.user),
    logout: publicProcedure.mutation(({ ctx }) => {
      const cookieOptions = getSessionCookieOptions(ctx.req);
      ctx.res.clearCookie(COOKIE_NAME, { ...cookieOptions, maxAge: -1 });
      return {
        success: true,
      } as const;
    }),
  }),

  clubs: router({
    sync: publicProcedure
      .input(z.object({ clubName: z.string(), platform: z.string().optional() }))
      .mutation(async ({ input }) => {
        const { syncClubData } = await import('./eafc-client');
        return syncClubData(input.clubName, input.platform || 'common-gen5');
      }),

    getLatest: publicProcedure.query(async () => {
      const { getLatestClub, getPlayersByClubId, getMatchesByClubId } = await import('./db');
      const club = await getLatestClub();
      if (!club) return null;

      const players = await getPlayersByClubId(club.clubId);
      const matches = await getMatchesByClubId(club.clubId, 20);

      const stats = {
        winRate: club.gamesPlayed ? Math.round(((club.wins || 0) / club.gamesPlayed) * 100) : 0,
        goalsPerGame: club.gamesPlayed ? ((club.goals || 0) / club.gamesPlayed).toFixed(2) : 0,
        goalsAgainstPerGame: club.gamesPlayed ? ((club.goalsAgainst || 0) / club.gamesPlayed).toFixed(2) : 0,
        goalDifference: (club.goals || 0) - (club.goalsAgainst || 0),
        totalPlayers: players.length,
      };

      return { club, players, matches, stats };
    }),

    analyzePlayer: publicProcedure
      .input(z.object({ playerName: z.string() }))
      .mutation(async ({ input }) => {
        const { getLatestClub, getPlayersByClubId, getMatchesByClubId } = await import('./db');
        const { analyzePlayerWithAI } = await import('./eafc-client');

        const club = await getLatestClub();
        if (!club) throw new Error('No club found');

        const players = await getPlayersByClubId(club.clubId);
        const player = players.find(p => p.playerName.toLowerCase() === input.playerName.toLowerCase());
        if (!player) throw new Error('Player not found');

        const matches = await getMatchesByClubId(club.clubId, 10);
        const analysis = await analyzePlayerWithAI(club.clubId, player.playerName, player, club, matches);

        return { player, analysis };
      }),

    generateIdealTeam: publicProcedure.mutation(async () => {
      const { getLatestClub, getPlayersByClubId, getMatchesByClubId } = await import('./db');
      const { generateIdealTeamWithAI } = await import('./eafc-client');

      const club = await getLatestClub();
      if (!club) throw new Error('No club found');

      const players = await getPlayersByClubId(club.clubId);
      const matches = await getMatchesByClubId(club.clubId, 20);
      const analysis = await generateIdealTeamWithAI(club.clubId, players, club, matches);

      return { analysis };
    }),
  }),
});

export type AppRouter = typeof appRouter;
