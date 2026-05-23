import { useState, useMemo } from 'react';
import { trpc } from '@/lib/trpc';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Card } from '@/components/ui/card';
import { Dialog, DialogContent, DialogHeader, DialogTitle } from '@/components/ui/dialog';
import { Loader2, RefreshCw, Search, Trophy, Users, Target } from 'lucide-react';
import { Streamdown } from 'streamdown';

export default function Dashboard() {
  const [syncLoading, setSyncLoading] = useState(false);
  const [playerSearch, setPlayerSearch] = useState('');
  const [selectedPlayer, setSelectedPlayer] = useState<string | null>(null);
  const [showPlayerAnalysis, setShowPlayerAnalysis] = useState(false);
  const [showTeamAnalysis, setShowTeamAnalysis] = useState(false);

  // Queries
  const { data: clubData, isLoading: isLoadingClub, refetch: refetchClub } = trpc.clubs.getLatest.useQuery();
  const syncMutation = trpc.clubs.sync.useMutation();
  const analyzePlayerMutation = trpc.clubs.analyzePlayer.useMutation();
  const generateTeamMutation = trpc.clubs.generateIdealTeam.useMutation();

  // Handle club sync
  const handleSync = async () => {
    setSyncLoading(true);
    try {
      await syncMutation.mutateAsync({ clubName: 'DESAGREGADOS SC', platform: 'common-gen5' });
      await refetchClub();
    } finally {
      setSyncLoading(false);
    }
  };

  // Filter players by search
  const filteredPlayers = useMemo(() => {
    if (!clubData?.players) return [];
    return clubData.players.filter(p =>
      p.playerName.toLowerCase().includes(playerSearch.toLowerCase())
    ).sort((a, b) => (b.rating || 0) - (a.rating || 0));
  }, [clubData?.players, playerSearch]);

  // Handle player analysis
  const handleAnalyzePlayer = async (playerName: string) => {
    setSelectedPlayer(playerName);
    setShowPlayerAnalysis(true);
    await analyzePlayerMutation.mutateAsync({ playerName });
  };

  // Handle team generation
  const handleGenerateTeam = async () => {
    setShowTeamAnalysis(true);
    await generateTeamMutation.mutateAsync();
  };

  if (isLoadingClub) {
    return (
      <div className="flex items-center justify-center min-h-screen">
        <Loader2 className="w-8 h-8 animate-spin text-accent" />
      </div>
    );
  }

  if (!clubData?.club) {
    return (
      <div className="flex items-center justify-center min-h-screen">
        <Card className="w-full max-w-md p-8 text-center">
          <Trophy className="w-16 h-16 mx-auto mb-4 text-accent" />
          <h2 className="text-2xl font-bold mb-2">Nenhum clube sincronizado</h2>
          <p className="text-muted-foreground mb-6">
            Sincronize um clube da EA FC para começar a visualizar estatísticas e análises.
          </p>
          <Button
            onClick={handleSync}
            disabled={syncLoading}
            className="w-full accent-bg"
          >
            {syncLoading ? (
              <>
                <Loader2 className="w-4 h-4 mr-2 animate-spin" />
                Sincronizando...
              </>
            ) : (
              <>
                <RefreshCw className="w-4 h-4 mr-2" />
                Sincronizar Clube
              </>
            )}
          </Button>
        </Card>
      </div>
    );
  }

  const { club, stats, players, matches } = clubData;

  return (
    <div className="space-y-6">
      {/* Header */}
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-3xl font-bold text-accent">{club.name}</h1>
          <p className="text-muted-foreground text-sm">
            Plataforma: {club.platform} • Estádio: {club.stadium || 'N/A'}
          </p>
        </div>
        <Button
          onClick={handleSync}
          disabled={syncLoading}
          variant="outline"
          className="accent-border accent-text hover:accent-bg"
        >
          {syncLoading ? (
            <>
              <Loader2 className="w-4 h-4 mr-2 animate-spin" />
              Sincronizando...
            </>
          ) : (
            <>
              <RefreshCw className="w-4 h-4 mr-2" />
              Atualizar Dados
            </>
          )}
        </Button>
      </div>

      {/* Statistics Grid */}
      <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-4 gap-4">
        <Card className="card-stat">
          <span>Taxa de Vitória</span>
          <strong>{stats?.winRate || 0}%</strong>
        </Card>
        <Card className="card-stat">
          <span>Jogos</span>
          <strong>{club.gamesPlayed}</strong>
        </Card>
        <Card className="card-stat">
          <span>Vitórias</span>
          <strong className="text-accent">{club.wins}</strong>
        </Card>
        <Card className="card-stat">
          <span>Empates</span>
          <strong>{club.ties}</strong>
        </Card>
        <Card className="card-stat">
          <span>Derrotas</span>
          <strong className="text-destructive">{club.losses}</strong>
        </Card>
        <Card className="card-stat">
          <span>Gols Pró</span>
          <strong>{club.goals}</strong>
        </Card>
        <Card className="card-stat">
          <span>Gols Contra</span>
          <strong className="text-destructive">{club.goalsAgainst}</strong>
        </Card>
        <Card className="card-stat">
          <span>Saldo</span>
          <strong className={stats?.goalDifference! > 0 ? 'text-accent' : 'text-destructive'}>
            {stats?.goalDifference}
          </strong>
        </Card>
        <Card className="card-stat">
          <span>Gols/Jogo</span>
          <strong>{stats?.goalsPerGame}</strong>
        </Card>
        <Card className="card-stat">
          <span>Sofridos/Jogo</span>
          <strong className="text-destructive">{stats?.goalsAgainstPerGame}</strong>
        </Card>
        <Card className="card-stat">
          <span>Clean Sheets</span>
          <strong>{club.cleanSheets}</strong>
        </Card>
        <Card className="card-stat">
          <span>Jogadores</span>
          <strong>{stats?.totalPlayers}</strong>
        </Card>
      </div>

      {/* Players Section */}
      <div className="space-y-4">
        <div className="flex items-center justify-between">
          <h2 className="text-2xl font-bold">Jogadores</h2>
          <Button
            onClick={handleGenerateTeam}
            disabled={generateTeamMutation.isPending}
            className="accent-bg"
          >
            {generateTeamMutation.isPending ? (
              <>
                <Loader2 className="w-4 h-4 mr-2 animate-spin" />
                Gerando...
              </>
            ) : (
              <>
                <Target className="w-4 h-4 mr-2" />
                Time Ideal com IA
              </>
            )}
          </Button>
        </div>

        {/* Search */}
        <div className="relative">
          <Search className="absolute left-3 top-1/2 transform -translate-y-1/2 w-4 h-4 text-muted-foreground" />
          <Input
            placeholder="Buscar jogador..."
            value={playerSearch}
            onChange={(e) => setPlayerSearch(e.target.value)}
            className="pl-10"
          />
        </div>

        {/* Players Table */}
        <div className="overflow-x-auto bg-card border border-border rounded-lg">
          <table className="w-full">
            <thead>
              <tr className="border-b border-border">
                <th className="px-4 py-3 text-left text-xs font-semibold text-accent uppercase tracking-wider">
                  Jogador
                </th>
                <th className="px-4 py-3 text-left text-xs font-semibold text-accent uppercase tracking-wider">
                  Posição
                </th>
                <th className="px-4 py-3 text-left text-xs font-semibold text-accent uppercase tracking-wider">
                  Jogos
                </th>
                <th className="px-4 py-3 text-left text-xs font-semibold text-accent uppercase tracking-wider">
                  Nota
                </th>
                <th className="px-4 py-3 text-left text-xs font-semibold text-accent uppercase tracking-wider">
                  Gols
                </th>
                <th className="px-4 py-3 text-left text-xs font-semibold text-accent uppercase tracking-wider">
                  Assist.
                </th>
                <th className="px-4 py-3 text-left text-xs font-semibold text-accent uppercase tracking-wider">
                  Pass%
                </th>
                <th className="px-4 py-3 text-left text-xs font-semibold text-accent uppercase tracking-wider">
                  Div%
                </th>
                <th className="px-4 py-3 text-left text-xs font-semibold text-accent uppercase tracking-wider">
                  MOM
                </th>
                <th className="px-4 py-3 text-left text-xs font-semibold text-accent uppercase tracking-wider">
                  Ação
                </th>
              </tr>
            </thead>
            <tbody>
              {filteredPlayers.length === 0 ? (
                <tr>
                  <td colSpan={10} className="px-4 py-8 text-center text-muted-foreground">
                    Nenhum jogador encontrado
                  </td>
                </tr>
              ) : (
                filteredPlayers.map((player) => (
                  <tr key={player.id} className="border-b border-border hover:bg-muted/30 transition-colors">
                    <td className="px-4 py-3 font-medium">{player.playerName}</td>
                    <td className="px-4 py-3 text-sm text-muted-foreground">{player.position || '-'}</td>
                    <td className="px-4 py-3 text-sm">{player.games}</td>
                    <td className="px-4 py-3 text-sm font-semibold text-accent">{player.rating}</td>
                    <td className="px-4 py-3 text-sm">{player.goals}</td>
                    <td className="px-4 py-3 text-sm">{player.assists}</td>
                    <td className="px-4 py-3 text-sm">{player.passPercent}%</td>
                    <td className="px-4 py-3 text-sm">{player.duelsPercent}%</td>
                    <td className="px-4 py-3 text-sm">{player.motm}</td>
                    <td className="px-4 py-3">
                      <Button
                        onClick={() => handleAnalyzePlayer(player.playerName)}
                        size="sm"
                        variant="outline"
                        className="accent-border accent-text hover:accent-bg text-xs"
                      >
                        IA
                      </Button>
                    </td>
                  </tr>
                ))
              )}
            </tbody>
          </table>
        </div>
      </div>

      {/* Matches Section */}
      <div className="space-y-4">
        <h2 className="text-2xl font-bold">Últimas Partidas</h2>
        <div className="overflow-x-auto bg-card border border-border rounded-lg">
          <table className="w-full">
            <thead>
              <tr className="border-b border-border">
                <th className="px-4 py-3 text-left text-xs font-semibold text-accent uppercase tracking-wider">
                  Adversário
                </th>
                <th className="px-4 py-3 text-left text-xs font-semibold text-accent uppercase tracking-wider">
                  Placar
                </th>
                <th className="px-4 py-3 text-left text-xs font-semibold text-accent uppercase tracking-wider">
                  Resultado
                </th>
                <th className="px-4 py-3 text-left text-xs font-semibold text-accent uppercase tracking-wider">
                  Tipo
                </th>
                <th className="px-4 py-3 text-left text-xs font-semibold text-accent uppercase tracking-wider">
                  Data
                </th>
              </tr>
            </thead>
            <tbody>
              {matches?.length === 0 ? (
                <tr>
                  <td colSpan={5} className="px-4 py-8 text-center text-muted-foreground">
                    Nenhuma partida registrada
                  </td>
                </tr>
              ) : (
                matches?.map((match) => (
                  <tr key={match.id} className="border-b border-border hover:bg-muted/30 transition-colors">
                    <td className="px-4 py-3 font-medium">{match.opponentName}</td>
                    <td className="px-4 py-3 font-semibold">
                      {match.goalsFor} × {match.goalsAgainst}
                    </td>
                    <td className="px-4 py-3">
                      {match.result === 'V' && <span className="badge-win">Vitória</span>}
                      {match.result === 'E' && <span className="badge-draw">Empate</span>}
                      {match.result === 'D' && <span className="badge-loss">Derrota</span>}
                    </td>
                    <td className="px-4 py-3 text-sm text-muted-foreground">
                      {match.matchType === 'leagueMatch' ? 'Liga' : 'Playoff'}
                    </td>
                    <td className="px-4 py-3 text-sm text-muted-foreground">{match.playedAt}</td>
                  </tr>
                ))
              )}
            </tbody>
          </table>
        </div>
      </div>

      {/* Player Analysis Dialog */}
      <Dialog open={showPlayerAnalysis} onOpenChange={setShowPlayerAnalysis}>
        <DialogContent className="max-w-2xl max-h-[80vh] overflow-y-auto">
          <DialogHeader>
            <DialogTitle className="text-accent">
              Análise de {selectedPlayer}
            </DialogTitle>
          </DialogHeader>
          {analyzePlayerMutation.isPending ? (
            <div className="flex items-center justify-center py-8">
              <Loader2 className="w-8 h-8 animate-spin text-accent" />
            </div>
          ) : analyzePlayerMutation.data?.analysis ? (
            <div className="prose prose-invert max-w-none">
              <Streamdown>{String(analyzePlayerMutation.data.analysis)}</Streamdown>
            </div>
          ) : null}
        </DialogContent>
      </Dialog>

      {/* Team Analysis Dialog */}
      <Dialog open={showTeamAnalysis} onOpenChange={setShowTeamAnalysis}>
        <DialogContent className="max-w-2xl max-h-[80vh] overflow-y-auto">
          <DialogHeader>
            <DialogTitle className="text-accent">
              Time Ideal - Análise de IA
            </DialogTitle>
          </DialogHeader>
          {generateTeamMutation.isPending ? (
            <div className="flex items-center justify-center py-8">
              <Loader2 className="w-8 h-8 animate-spin text-accent" />
            </div>
          ) : generateTeamMutation.data?.analysis ? (
            <div className="prose prose-invert max-w-none">
              <Streamdown>{String(generateTeamMutation.data.analysis)}</Streamdown>
            </div>
          ) : null}
        </DialogContent>
      </Dialog>
    </div>
  );
}
