# Intégrer le scanner à ton bot existant

## Option A : process séparé (recommandé)

Lance `python -m ticker_scanner` à côté de ton bot.
- **Même token** si ton bot ne fait qu'envoyer des messages Telegram.
- **2e bot** (@BotFather) si ton bot lit déjà des commandes Telegram.

Les notifs arrivent dans le chat défini par `TELEGRAM_NOTIFY_CHAT_ID`, qui peut être le même que celui des tweets.

## Option B : tout dans ton bot (Python uniquement)

Le scanner fournit un mode « embarqué » qui ne lit jamais les updates lui-même :

```python
from ticker_scanner.embed import start_embedded

scanner_app = await start_embedded()          # lit le même .env

# Pour chaque update reçue par ton bot (dict brut de l'API Telegram) :
if await scanner_app.handle_update(update_dict):
    return  # c'était pour le scanner (/scanner, /nouveau, boutons "sc:…")
```

Pour obtenir l'update en dict :
- **python-telegram-bot** : `update.to_dict()`, dans un `TypeHandler(Update, ...)` du groupe `-1`.
- **aiogram 3** : `update.model_dump(mode="json", exclude_none=True)`, dans un middleware.

Si ton bot est en Node.js, reste sur l'option A.

## Prompt à coller dans ton Claude Code local

Copie le dossier `telegram-ticker-scanner/` à côté du code de ton bot, puis colle ce prompt :

```
J'ai ajouté le dossier telegram-ticker-scanner/ (scanner de tickers Python, voir son README.md
et INTEGRATION.md). Mon bot actuel lit le flux TweetShift sur Discord, filtre les tweets avec
l'API Claude et les envoie sur Telegram.

1. Regarde comment mon bot utilise Telegram : est-ce qu'il lit des updates (polling getUpdates
   ou webhook) ou est-ce qu'il ne fait qu'envoyer des messages ? Quel langage/lib ?
2. S'il ne fait qu'envoyer : configure le scanner en process séparé avec le même
   TELEGRAM_BOT_TOKEN et le même chat de notification (option A), et ajoute son lancement
   là où mon bot est démarré (script, Procfile, service, Railway…).
3. S'il lit des updates et qu'il est en Python : intègre le scanner avec
   ticker_scanner.embed.start_embedded() et transmets chaque update à
   scanner_app.handle_update(update_dict) avant mes propres handlers (option B).
   Ne casse aucune commande existante de mon bot.
4. S'il lit des updates et n'est pas en Python : explique-moi comment créer un 2e bot
   avec @BotFather et configure le scanner avec ce token (option A).
5. Remplis le .env du scanner à partir de .env.example (sans inventer de clés : demande-moi
   celles qui manquent), installe requirements.txt et lance les tests :
   python -m unittest discover -s tests -t .
6. Lance le scanner et vérifie dans les logs que les sources démarrent.
```
