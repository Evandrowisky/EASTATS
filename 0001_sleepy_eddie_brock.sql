CREATE TABLE `aiReports` (
	`id` int AUTO_INCREMENT NOT NULL,
	`clubId` varchar(64) NOT NULL,
	`reportType` varchar(64) NOT NULL,
	`playerName` varchar(255),
	`prompt` text,
	`response` text,
	`createdAt` timestamp DEFAULT (now()),
	CONSTRAINT `aiReports_id` PRIMARY KEY(`id`)
);
--> statement-breakpoint
CREATE TABLE `clubs` (
	`id` int AUTO_INCREMENT NOT NULL,
	`clubId` varchar(64) NOT NULL,
	`name` varchar(255) NOT NULL,
	`platform` varchar(64) NOT NULL,
	`gamesPlayed` int DEFAULT 0,
	`wins` int DEFAULT 0,
	`ties` int DEFAULT 0,
	`losses` int DEFAULT 0,
	`goals` int DEFAULT 0,
	`goalsAgainst` int DEFAULT 0,
	`cleanSheets` int DEFAULT 0,
	`points` int DEFAULT 0,
	`bestDivision` varchar(255),
	`stadium` varchar(255),
	`rawJson` text,
	`updatedAt` timestamp DEFAULT (now()) ON UPDATE CURRENT_TIMESTAMP,
	`createdAt` timestamp DEFAULT (now()),
	CONSTRAINT `clubs_id` PRIMARY KEY(`id`),
	CONSTRAINT `clubs_clubId_unique` UNIQUE(`clubId`)
);
--> statement-breakpoint
CREATE TABLE `matches` (
	`id` int AUTO_INCREMENT NOT NULL,
	`clubId` varchar(64) NOT NULL,
	`matchId` varchar(255) NOT NULL,
	`opponentName` varchar(255) NOT NULL,
	`goalsFor` int DEFAULT 0,
	`goalsAgainst` int DEFAULT 0,
	`result` varchar(10),
	`matchType` varchar(64),
	`playedAt` varchar(255),
	`rawJson` text,
	`createdAt` timestamp DEFAULT (now()),
	CONSTRAINT `matches_id` PRIMARY KEY(`id`)
);
--> statement-breakpoint
CREATE TABLE `players` (
	`id` int AUTO_INCREMENT NOT NULL,
	`clubId` varchar(64) NOT NULL,
	`playerName` varchar(255) NOT NULL,
	`position` varchar(64),
	`games` int DEFAULT 0,
	`rating` int DEFAULT 0,
	`goals` int DEFAULT 0,
	`assists` int DEFAULT 0,
	`passPercent` int DEFAULT 0,
	`duelsPercent` int DEFAULT 0,
	`shots` int DEFAULT 0,
	`saves` int DEFAULT 0,
	`motm` int DEFAULT 0,
	`rawJson` text,
	`updatedAt` timestamp DEFAULT (now()) ON UPDATE CURRENT_TIMESTAMP,
	CONSTRAINT `players_id` PRIMARY KEY(`id`)
);
