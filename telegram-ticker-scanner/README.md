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

**Liquidité** : après un 🟡 PAIR CREATED, le scanner surveille la pair pendant 72 h et t'envoie 🟢 LIQ ADDED dès que de la liquidité réelle arrive. Pour les pairs v2, il vérifie les réserves, et pas seulement des jetons envoyés à la pair.

**Lancement furtif** : l'équipe crée la pair des jours avant et n'ajoute la liquidité qu'au lancement. Le scanner l'attrape même si la pair existait avant ton projet :
- **en temps réel on-chain, v2, v3 et v4**. Dans les trois cas, la factory peut être inconnue et le pool ancien :
  - **v2** (Uniswap, PancakeSwap, SushiSwap et leurs forks) : la toute première liquidité d'une pair laisse une empreinte unique ;
  - **v3** (Uniswap, PancakeSwap, Aerodrome CL et leurs forks) : chaque ajout de liquidité sur un pool est écouté. Si le pool ne contenait aucun de ses deux tokens au bloc précédent, c'est sa première liquidité ;
  - **v4** (Uniswap v4) : chaque ajout de liquidité sur le PoolManager est écouté. Les tokens du pool viennent du PositionManager d'Uniswap. La clé est vérifiée par hachage, donc un contrat malveillant ne peut pas faire passer un faux pool. L'état du pool au bloc précédent est lu directement dans le stockage du contrat. Sur un pool de launchpad (hook Pons), l'alerte est un 🟣 TOKEN CREATED.

  Un pool déjà actif n'est jamais signalé : seule sa toute première liquidité compte. Le scanner envoie 🟢 LIQ ADDED, ou 🟡 + 🟢 si le pool a été créé dans la même transaction ;
- **via DexScreener** : à l'ajout d'un ticker, le scanner photographie les pairs déjà existantes. Toute pair absente de cette photo qui apparaît ensuite est signalée, même si sa date de création est ancienne.

**Anti-flood** : quand un ticker est à la mode, des dizaines de copies sortent en quelques minutes. Au-delà de 15 alertes en 10 min pour un même projet, les suivantes sont regroupées dans un résumé. Celles avec plus de 10 000 $ de liquidité passent toujours, et tout reste dans 📋 Résultats. Les deux seuils se règlent dans `.env`.

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
- **Copies du même ticker** : elles sont toutes enregistrées. Les notifications sont regroupées en cas de rafale (voir Anti-flood). Le market cap, la liquidité et le launchpad t'aident à repérer le vrai token.
- **Paires antérieures** : les pairs qui existaient déjà avec de la liquidité avant l'ajout du projet ne sont pas notifiées. Les lancements furtifs le sont.
- **Groupes** : le bot ne répond qu'aux comptes listés dans `TELEGRAM_ALLOWED_USERS`, et ignore `/scanner@AutreBot`.
- **Adresses à vérifier** : les adresses des factories et launchpads viennent de sources publiques (docs Uniswap, Bitquery, BscScan…). Le scanner n'a pas pu être lancé contre les vraies blockchains depuis l'environnement où il a été écrit, faute d'accès réseau. Au premier lancement, regarde les logs (`watcher on-chain démarré`, `websocket connecté`). En cas de souci, `LOG_LEVEL=DEBUG` donne le détail.
- **Lien Defined pour Robinhood** : il utilise le slug `robinhood`. S'il ne fonctionne pas, corrige `defined=` dans `chains.py`.

## Fiabilité

Le scanner est conçu pour tourner des semaines sans surveillance.

- **Reconnexion automatique** de chaque websocket, avec un délai croissant. Une connexion restée muette 2 à 3 minutes (fournisseur qui ne transmet plus rien sans couper) est rouverte.
- **Rattrapage** : après une coupure, les 10 dernières minutes sont relues via `eth_getLogs`, quelle que soit la vitesse des blocs (6 000 blocs sur Robinhood). Si le RPC refuse une plage trop grande, elle est découpée automatiquement. Quand le fournisseur annonce sa limite (« up to a 10 block range »), elle est reprise telle quelle.
- **Reconnexion immédiate** quand la connexion précédente transmettait des données. Le délai croissant ne s'applique qu'aux échecs répétés.
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
- **Secrets masqués** : le token du bot et les clés API sont remplacés par `***` dans les logs et sur la page 🩺 État. Un avertissement s'affiche si `.env` est lisible par d'autres utilisateurs (`chmod 600 .env`).
- **Données externes validées** : toute adresse venant de DexScreener, PumpPortal ou d'un RPC doit avoir un format EVM ou Solana strict, sinon elle est rejetée. Une donnée forgée ne peut donc ni casser l'affichage ni injecter un lien.
- **Noms de tokens piégés** : caractères de contrôle et inversion du sens du texte sont retirés, et les noms sont tronqués.
- **Clics rejoués** : la position de lecture Telegram est mémorisée et les boutons portent l'état voulu. Un redémarrage ne rejoue donc jamais un clic (par exemple réactiver un projet en pause). Un ancien sélecteur de chaînes ne peut pas modifier le mauvais projet.
- **Configuration** : une valeur invalide dans `.env` est signalée au démarrage avec un message clair.
- **Arrêt propre** sur Ctrl+C ou SIGTERM (Docker, Railway, systemd).
- **Redémarrage** : si l'ancienne instance occupe encore Telegram quelques secondes, le menu réessaie au lieu de se désactiver.

## Performance

- **Latence** :
  - **EVM** : les appels RPC nécessaires après un ticker reconnu (nom, contrôle de nouveauté, reçu) partent en parallèle. Le reçu est mis en cache et partagé entre les détecteurs. Les events de pairs passent avant le flux de mints.
  - **DexScreener** : jusqu'à 4 recherches en parallèle, dans la limite de 250 requêtes/min.
- **Débit mesuré** :
  - environ 2 800 logs de mint EVM/s, avec 1 appel RPC par nouveau token et 0 pour un token déjà vu ;
  - environ 6 800 ajouts de liquidité v3/s, avec 3 appels RPC par nouveau pool et 0 ensuite ;
  - environ 3 600 transactions Solana/s analysées.
- **Mémoire bornée** : environ 35 Mo maximum par chaîne, quelle que soit la durée de fonctionnement.
- **Accélérateurs optionnels** : `pip install uvloop orjson`. Ils sont utilisés automatiquement s'ils sont installés.

## Tests

```bash
python -m unittest discover -s tests -t .
FUZZ_ITERATIONS=20000 python -m unittest tests.test_fuzz   # fuzzing approfondi
```

Les 142 tests passent sur Python 3.10, 3.11, 3.12 et 3.13. Ils simulent le RPC EVM, les websockets, DexScreener, PumpPortal, Solana et Telegram, et couvrent :
- **bout en bout** : création d'un projet depuis Telegram, démarrage des sources, notification reçue ;
- **chaos** : l'application complète contre des services qui tombent en panne au hasard (environ 30 % d'erreurs 5xx, 429 et réseau), avec websockets coupés et événements rejoués. La notification arrive exactement une fois, rien ne plante ;
- **fuzzing** : des milliers d'entrées aléatoires et malveillantes envoyées à chaque parseur et à chaque point d'entrée (logs EVM, transactions Solana, PumpPortal, DexScreener, updates Telegram). Aucun plantage, et le HTML produit est toujours valide pour Telegram ;
- **robustesse et sécurité** : coupures, rate-limits, annulations concurrentes, conflits Telegram, adresses forgées, secrets, lancements furtifs (v2, v3, v4), clés de pool v4 usurpées, anti-flood.
