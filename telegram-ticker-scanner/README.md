# Scanner de tickers pour Telegram

Tu entres des projets dans ton bot Telegram (un nom, 1 à 3 tickers, une ou plusieurs blockchains). Le scanner t'envoie une notification dès que :

| Label | Événement |
|---|---|
| 🟣 **TOKEN CREATED** | un token avec ce ticker est créé, sur un launchpad (pump.fun, bonk.fun, StonkFun, Pons, LONG, o1, PAIR, Flap, four.meme…) ou par déploiement direct |
| 🟡 **PAIR CREATED** | une pair ou un pool est créé sur un DEX |
| 🟢 **LIQ ADDED** | de la liquidité est ajoutée à cette pair |

Si la pair et la liquidité arrivent dans la même transaction, tu reçois une seule notif avec les deux labels : `🟡 PAIR CREATED + 🟢 LIQ ADDED`.

Chaque notification affiche :
- un encadré monospace (sombre en mode sombre) qui la distingue des notifs de tweets ;
- le contrat et la pair, copiables d'un tap ;
- la liquidité et le market cap, complétés automatiquement après coup ;
- des boutons DexScreener, Defined et GMGN ;
- un bouton « Résultats du projet ».

```
🟡 PAIR CREATED + 🟢 LIQ ADDED
━━━━━━━━━━━━━━━━━━━━━━
Projet : Mon projet
Ticker : $MOON
Nom    : Moon
Chaîne : BSC
DEX    : PancakeSwap v2 · /WBNB
Liq    : $30K
MC     : $250K
━━━━━━━━━━━━━━━━━━━━━━
📄 CA : 0x…            (tap = copier)
🔗 Pair : 0x…
[DexScreener] [Defined] [GMGN]
[📋 Résultats · Mon projet]
```

> Telegram ne permet pas de colorer le fond d'un message. Le bloc monospace est ce qui se rapproche le plus d'une « box noire ». Les couleurs des labels viennent des emojis 🟣🟡🟢.

## Menu Telegram

- `/scanner` : liste des projets. Pour chaque projet :
  - ⏸ pause ou ▶️ reprise ;
  - ✏️ modification du nom, des tickers ou des chaînes ;
  - 🗑 suppression ;
  - 📋 résultats.
- `/nouveau` : ajouter un projet. Le bot demande le nom, puis 1 à 3 tickers, puis les blockchains à cocher (boutons « Toutes EVM » et « Tout » disponibles).
- `/annuler` : annuler la saisie en cours.
- `/scanner_etat` (ou le bouton **🩺 État** du menu) : santé de chaque source en direct. Pour chacune, tu vois :
  - si elle est connectée ;
  - le nombre d'événements reçus et le dernier en date ;
  - le nombre de détections ;
  - la dernière erreur.

  On y trouve aussi les notifs envoyées ou perdues et les avertissements de configuration. Le bouton **🔔 Envoyer une notif de test** permet de vérifier le format et le chat de destination.
- **Résultats** : liste paginée, du plus récent au plus ancien. Pour chaque résultat :
  - le statut 🟣/🟡/🟢 ;
  - la chaîne, le DEX ou launchpad, et l'âge ;
  - la liquidité et le market cap ;
  - le contrat, copiable ;
  - les liens DexScreener, Defined et GMGN.

  Le bouton **🔄 Refresh** recharge la liquidité et les market caps en temps réel.

## Ce qui est surveillé

Chaque chaîne est scannée via **DexScreener** (toutes les chaînes, sans clé, avec un délai de quelques secondes à environ une minute). Si tu configures un RPC, s'ajoute une détection **on-chain en temps réel** (⚡ dans le menu) :

| Chaîne | Temps réel (⚡) | Clé nécessaire |
|---|---|---|
| **Solana** | pump.fun et bonk.fun via PumpPortal | aucune |
| | LaunchLab (bonk.fun, **StonkFun**), Meteora DBC (Bags, Believe, Jup Studio…), Moonshot, Boop | Helius (gratuit) |
| | Raydium, Meteora, PumpSwap, Orca (pools directs et migrations) | DexScreener (aucune) |
| **Robinhood** | tout nouveau token (Pons, LONG, o1, PAIR, Flap, Bankr, hood.fun…) + Uniswap v2/v3/v4 | RPC public inclus, ou Alchemy |
| **BSC** | tout nouveau token (four.meme, Flap…) + PancakeSwap v2/v3, Uniswap v2/v3/v4 | Alchemy (gratuit) ou autre RPC |
| **Ethereum, Base, Arbitrum** | tout nouveau token + Uniswap v2/v3/v4, SushiSwap, PancakeSwap v3, Aerodrome (Base) | idem |
| Polygon, Avalanche, Optimism, Unichain, Sonic, Abstract, HyperEVM, Monad, Linea, Blast, Mantle, Berachain, zkSync | DexScreener seulement (🐢) | aucune |

**« Tout nouveau token »** (EVM) : le scanner écoute toutes les créations de tokens ERC-20 de la chaîne. Il détecte donc les tokens de **n'importe quel launchpad**, même inconnu. Quand l'adresse du launchpad est connue (Pons, PAIR, Flap, four.meme), son nom s'affiche. Sinon tu vois `Nouveau token · via 0x1234…abcd`, c'est-à-dire le contrat appelé.

**Liquidité** : après un 🟡 PAIR CREATED, le scanner surveille la pair pendant 72 h et t'envoie 🟢 LIQ ADDED dès que de la liquidité arrive.

Pour ajouter une chaîne EVM au temps réel, il suffit d'une entrée `Chain(...)` dans `ticker_scanner/chains.py` (adresses des factories) et des variables `<CHAÎNE>_WS_URL` dans `.env`.

## Installation

Il te faut Python 3.10 ou plus.

```bash
cd telegram-ticker-scanner
python -m venv .venv
source .venv/bin/activate          # Windows : .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env               # puis remplis .env
python -m ticker_scanner
```

1. Mets `TELEGRAM_BOT_TOKEN`, lance le scanner, puis envoie `/start` au bot. Il te répond avec ton ID Telegram.
2. Mets cet ID dans `TELEGRAM_ALLOWED_USERS`, relance, puis envoie `/scanner`.
3. Optionnel, pour le temps réel :
   - **Alchemy** (alchemy.com, gratuit) : crée une app, active BNB Chain, Ethereum, Base, Arbitrum et Robinhood si proposée. Copie les URLs **WSS** dans `BSC_WS_URL`, `ETHEREUM_WS_URL`, etc.
   - **Helius** (helius.dev, gratuit) : `SOLANA_WS_URL=wss://mainnet.helius-rpc.com/?api-key=TA_CLE`.
   - **Robinhood** : le RPC public officiel est déjà dans `.env.example` (mode polling). Une URL WSS Alchemy est plus rapide.

Le scanner doit tourner en continu, sur le même serveur que ton bot de tweets, un VPS ou Railway. Les données sont stockées dans `ticker_scanner.db` (SQLite).

### Quotas gratuits

Le scanner n'écoute que les chaînes utilisées par au moins un projet actif. Sur les chaînes très actives (Robinhood, BSC), chaque nouveau token coûte un appel RPC (`symbol()`). Si l'offre gratuite d'Alchemy ne suffit plus :
- garde Alchemy en **WSS** ;
- mets un RPC public en **HTTP** pour les appels, par exemple `BSC_HTTP_URL=https://bsc-rpc.publicnode.com`.

## Même bot que les tweets ?

Le scanner peut utiliser **le même token** que ton bot actuel, à une condition : ton bot actuel ne fait qu'**envoyer** des messages sur Telegram et ne lit pas les commandes.

Si ton bot lit déjà des commandes Telegram, Telegram refuse que deux programmes lisent le même bot. Le scanner le détecte : il désactive son menu, garde les notifications actives et t'envoie un message d'explication. Deux solutions :

1. **Le plus simple** : crée un 2e bot avec @BotFather, juste pour le scanner, et mets son token dans `.env`. Les notifs arrivent alors d'un expéditeur différent, ce qui les distingue encore mieux des tweets.
2. **Tout dans un seul bot** : voir [INTEGRATION.md](INTEGRATION.md). Il contient un prompt prêt à coller dans ton Claude Code local.

## Bon à savoir

- **Correspondance des tickers** : exacte, sans tenir compte des majuscules ni du `$` (`$moon` = `MOON`).
- **Copies du même ticker** : elles sont **toutes** notifiées, et sur Solana et Robinhood il y en a souvent plusieurs. Le market cap, la liquidité et le launchpad t'aident à repérer le vrai.
- **Paires antérieures** : les paires créées avant l'ajout du projet ne sont pas notifiées.
- **Adresses à vérifier** : les adresses des factories et launchpads viennent de sources publiques (docs Uniswap, Bitquery, BscScan…). Le scanner n'a pas pu être lancé contre les vraies blockchains depuis l'environnement où il a été écrit, faute d'accès réseau. Au premier lancement, regarde les logs (`watcher on-chain démarré`, `websocket connecté`). En cas de souci, `LOG_LEVEL=DEBUG` donne le détail.
- **Lien Defined pour Robinhood** : il utilise le slug `robinhood`. S'il ne fonctionne pas, corrige `defined=` dans `chains.py`.

## Fiabilité

Le scanner est conçu pour tourner des semaines sans surveillance.

- **Reconnexion automatique** de chaque websocket, avec un délai croissant. Une connexion restée muette 2 à 3 minutes (fournisseur qui ne transmet plus rien sans couper) est rouverte.
- **Rattrapage** : après une coupure, les blocs manqués sont relus via `eth_getLogs`. Si le RPC refuse une plage trop grande, elle est découpée automatiquement.
- **Erreurs RPC triées** : un rate-limit ou une panne réseau déclenche un nouvel essai. Un token n'est jamais écarté à tort parce que le RPC était saturé.
- **Notifications** :
  - nouvel essai en cas de coupure réseau ;
  - repli en texte brut si Telegram refuse le HTML ;
  - envoi sans boutons si un lien est refusé ;
  - message d'erreur clair si le bot ne peut pas écrire dans le chat.
- **Boucles relancées** : si une boucle interne plante, elle redémarre seule. Le reste continue de tourner.
- **Vérification au démarrage** :
  - le token Telegram ;
  - le chain ID de chaque RPC. Une URL Ethereum mise par erreur dans `BSC_WS_URL` désactive le temps réel BSC, avec un avertissement dans 🩺 État.
- **Secrets masqués** : le token du bot et les clés API sont remplacés par `***` dans les logs.
- **Noms de tokens piégés** : caractères de contrôle et inversion du sens du texte sont retirés, et les noms sont tronqués.
- **Configuration** : une valeur invalide dans `.env` est signalée au démarrage avec un message clair.
- **Arrêt propre** sur Ctrl+C ou SIGTERM (Docker, Railway, systemd).
- **Redémarrage** : si l'ancienne instance occupe encore Telegram quelques secondes, le menu réessaie au lieu de se désactiver.

## Tests

```bash
python -m unittest discover -s tests -t .
```

Les 87 tests passent sur Python 3.10, 3.11, 3.12 et 3.13. Ils simulent le RPC EVM, les websockets, DexScreener, PumpPortal, Solana et Telegram, et couvrent :
- un scénario de bout en bout : création d'un projet depuis Telegram, démarrage des sources, notification reçue ;
- chaque correction de robustesse : coupures, rate-limits, annulations concurrentes, payloads malformés, conflits Telegram.
