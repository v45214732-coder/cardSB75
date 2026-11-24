import os, json, asyncio, logging, sqlite3, datetime, re
import httpx
from fastapi import FastAPI, Request
import uvicorn
from telegram import Update, ReplyKeyboardMarkup
from telegram.ext import (Application, CommandHandler, MessageHandler,
                          ConversationHandler, ContextTypes, filters)

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("bot")
# httpx écrit l'URL complète (avec le token du bot) dans les logs : on le fait taire
logging.getLogger("httpx").setLevel(logging.WARNING)

# ---------- Configuration (variables d'environnement Railway) ----------
BOT_TOKEN      = os.environ["TELEGRAM_BOT_TOKEN"]
ZIINA_API_KEY  = os.environ["ZIINA_API_KEY"]
ADMIN_CHAT_ID  = int(os.environ["ADMIN_CHAT_ID"])
PUBLIC_URL     = os.environ["PUBLIC_URL"].rstrip("/")      # ex: https://xxx.up.railway.app
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "change-moi")
ZIINA_TEST     = os.environ.get("ZIINA_TEST", "true").lower() == "true"
DB_PATH        = os.environ.get("DB_PATH", "/data/ventes.db")
# Optionnel : liste des identifiants Telegram autorisés, ex: {"123456789": "Karim"}
# Si vide, tout le monde peut utiliser le bot.
AGENTS         = json.loads(os.environ.get("AGENTS") or "{}")
CURRENCY       = "EUR"
ZIINA_API      = "https://api-v2.ziina.com/api"


# ---------- Base de données ----------
def db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    con = sqlite3.connect(DB_PATH)
    con.execute("""CREATE TABLE IF NOT EXISTS ventes (
        id TEXT PRIMARY KEY,
        agent_id INTEGER, agent_nom TEXT, joueur TEXT,
        montant_cents INTEGER, statut TEXT,
        cree_le TEXT, paye_le TEXT, lien TEXT)""")
    return con


def autorise(user_id: int):
    return not AGENTS or str(user_id) in AGENTS


# ---------- Ziina ----------
async def ziina_create_payment(amount_cents: int, message: str):
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.post(f"{ZIINA_API}/payment_intent",
            headers={"Authorization": f"Bearer {ZIINA_API_KEY}"},
            json={"amount": amount_cents, "currency_code": CURRENCY,
                  "message": message, "test": ZIINA_TEST,
                  "success_url": f"{PUBLIC_URL}/merci",
                  "cancel_url":  f"{PUBLIC_URL}/annule",
                  "failure_url": f"{PUBLIC_URL}/echec"})
        if r.status_code >= 400:
            log.error("Ziina a refusé la création: %s %s", r.status_code, r.text)
        r.raise_for_status()
        return r.json()


async def ziina_get_payment(pi_id: str):
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.get(f"{ZIINA_API}/payment_intent/{pi_id}",
                        headers={"Authorization": f"Bearer {ZIINA_API_KEY}"})
        r.raise_for_status()
        return r.json()


async def ziina_register_webhook():
    try:
        async with httpx.AsyncClient(timeout=30) as c:
            r = await c.post(f"{ZIINA_API}/webhook",
                headers={"Authorization": f"Bearer {ZIINA_API_KEY}"},
                json={"url": f"{PUBLIC_URL}/ziina/webhook", "secret": WEBHOOK_SECRET})
            log.info("Webhook Ziina enregistré: %s %s", r.status_code, r.text)
    except Exception:
        log.exception("Impossible d'enregistrer le webhook Ziina")


# ---------- Conversation Telegram ----------
AGENT, JOUEUR, MONTANT = range(3)

# Boutons affichés sous la zone de saisie
BTN_RECOMMENCER = "🔄 Recommencer"
BTN_ANNULER     = "❌ Annuler"
F_RECOMMENCER = filters.Regex(r"(?i)^\W*recommencer$")
F_ANNULER     = filters.Regex(r"(?i)^\W*annuler$")
SAISIE = filters.TEXT & ~filters.COMMAND & ~F_RECOMMENCER & ~F_ANNULER
CLAVIER_EN_COURS = ReplyKeyboardMarkup([[BTN_RECOMMENCER, BTN_ANNULER]], resize_keyboard=True)
CLAVIER_FIN      = ReplyKeyboardMarkup([[BTN_RECOMMENCER]], resize_keyboard=True)


async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not autorise(update.effective_user.id):
        await update.message.reply_text(
            f"Accès refusé. Ton identifiant Telegram est {update.effective_user.id}, "
            "transmets-le à l'administrateur.")
        return ConversationHandler.END
    ctx.user_data.clear()
    await update.message.reply_text("Quel est ton nom de TeamLeader ou d'agent ?",
                                    reply_markup=CLAVIER_EN_COURS)
    return AGENT


async def get_agent(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    nom = " ".join(update.message.text.split()).title()
    if len(nom) < 2:
        await update.message.reply_text("Nom trop court, réessaie.")
        return AGENT
    ctx.user_data["agent"] = nom
    await update.message.reply_text("Nom du joueur ?")
    return JOUEUR


async def get_joueur(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    joueur = update.message.text.strip()
    if len(joueur) < 2:
        await update.message.reply_text("Nom trop court, réessaie.")
        return JOUEUR
    ctx.user_data["joueur"] = joueur
    await update.message.reply_text("Montant en euros ? (exemple : 100)")
    return MONTANT


async def get_montant(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    txt = update.message.text.strip().replace(",", ".").replace("€", "").strip()
    if not re.fullmatch(r"\d+(\.\d{1,2})?", txt) or float(txt) <= 0:
        await update.message.reply_text("Montant invalide. Exemple : 100")
        return MONTANT
    cents = round(float(txt) * 100)
    agent_id = update.effective_user.id
    nom = ctx.user_data["agent"]
    joueur = ctx.user_data["joueur"]
    u = update.effective_user
    compte = f"@{u.username}" if u.username else u.full_name
    try:
        pi = await ziina_create_payment(cents, f"{joueur} - Team Leader ou Agent : {nom}")
    except Exception:
        log.exception("Erreur Ziina")
        await update.message.reply_text(
            "Erreur lors de la création du lien, réessaie plus tard.",
            reply_markup=CLAVIER_FIN)
        return ConversationHandler.END
    lien = pi["redirect_url"]
    with db() as con:
        con.execute("INSERT INTO ventes VALUES (?,?,?,?,?,?,?,?,?)",
            (pi["id"], agent_id, nom, joueur, cents, "en_attente",
             datetime.datetime.now().isoformat(timespec="seconds"), None, lien))
    await update.message.reply_text(
        f"✅ Lien créé\n\nTeam Leader ou Agent : {nom}\nJoueur : {joueur}\n"
        f"Montant : {cents/100:.2f} €\n\n"
        f"Lien à envoyer au client :\n{lien}\n\n"
        "Tu recevras une confirmation dès que le paiement est effectué.",
        reply_markup=CLAVIER_FIN)
    return ConversationHandler.END


async def cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Annulé. Appuie sur « Recommencer » pour créer un nouveau lien.",
        reply_markup=CLAVIER_FIN)
    return ConversationHandler.END


async def mes_ventes(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not autorise(update.effective_user.id):
        return
    with db() as con:
        rows = con.execute("""SELECT joueur, montant_cents, statut, cree_le FROM ventes
                              WHERE agent_id=? ORDER BY cree_le DESC LIMIT 20""",
                           (update.effective_user.id,)).fetchall()
    if not rows:
        await update.message.reply_text("Aucune vente pour le moment.")
        return
    lignes = [f"{'✅' if s == 'paye' else '⏳' if s == 'en_attente' else '❌'} "
              f"{d[:10]} {j} — {m/100:.2f} €"
              for j, m, s, d in rows]
    await update.message.reply_text("Tes 20 dernières ventes :\n" + "\n".join(lignes))


async def recap(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id != ADMIN_CHAT_ID:
        return
    periode = (ctx.args[0] if ctx.args else "jour").lower()
    today = datetime.date.today()
    if periode == "semaine":
        depuis = (today - datetime.timedelta(days=today.weekday())).isoformat()
    elif periode == "mois":
        depuis = today.replace(day=1).isoformat()
    elif periode == "tout":
        depuis = "2000-01-01"
    else:
        depuis = today.isoformat()
    with db() as con:
        rows = con.execute("""SELECT agent_nom, COUNT(*), SUM(montant_cents) FROM ventes
                              WHERE statut='paye' AND paye_le >= ? GROUP BY agent_nom
                              ORDER BY SUM(montant_cents) DESC""", (depuis,)).fetchall()
        attente = con.execute(
            "SELECT COUNT(*) FROM ventes WHERE statut='en_attente' AND cree_le >= ?",
            (depuis,)).fetchone()[0]
    total = sum(r[2] for r in rows) if rows else 0
    lignes = [f"• {a} : {n} vente(s) — {m/100:.2f} €" for a, n, m in rows] \
        or ["Aucun paiement."]
    await update.message.reply_text(
        f"📊 Récap ({periode}, depuis le {depuis})\n" + "\n".join(lignes) +
        f"\n\nTotal encaissé : {total/100:.2f} €\nLiens en attente : {attente}")


def build_app():
    app = Application.builder().token(BOT_TOKEN).build()
    conv = ConversationHandler(
        entry_points=[CommandHandler("start", start), CommandHandler("vente", start),
                      MessageHandler(F_RECOMMENCER, start)],
        states={AGENT:   [MessageHandler(SAISIE, get_agent)],
                JOUEUR:  [MessageHandler(SAISIE, get_joueur)],
                MONTANT: [MessageHandler(SAISIE, get_montant)]},
        fallbacks=[CommandHandler("annuler", cancel), MessageHandler(F_ANNULER, cancel)],
        allow_reentry=True)   # /start relance le parcours même s'il était en cours
    app.add_handler(conv)
    # « Annuler » appuyé alors qu'aucun parcours n'est en cours
    app.add_handler(MessageHandler(F_ANNULER, cancel))
    app.add_handler(CommandHandler("annuler", cancel))
    app.add_handler(CommandHandler("mesventes", mes_ventes))
    app.add_handler(CommandHandler("recap", recap))
    return app


# ---------- Serveur web (webhook Ziina) ----------
api = FastAPI()
tg_app = build_app()


@api.get("/merci")
async def merci():
    return {"message": "Paiement reçu, merci !"}


@api.get("/annule")
async def annule():
    return {"message": "Paiement annulé."}


@api.get("/echec")
async def echec():
    return {"message": "Le paiement a échoué."}


@api.post("/ziina/webhook")
async def ziina_webhook(req: Request):
    body = await req.json()
    log.info("Webhook Ziina: %s", body)
    pi_id = (body.get("data") or {}).get("id")
    if not pi_id:
        return {"ok": True}
    # On ne fait jamais confiance au corps : on relit le paiement chez Ziina
    pi = await ziina_get_payment(pi_id)
    statut = pi.get("status")
    with db() as con:
        row = con.execute(
            "SELECT agent_id, agent_nom, joueur, montant_cents, statut FROM ventes WHERE id=?",
            (pi_id,)).fetchone()
        if not row:
            return {"ok": True}
        agent_id, nom, joueur, cents, ancien = row
        if ancien == "paye":
            return {"ok": True}          # déjà traité
        if statut == "completed":
            con.execute("UPDATE ventes SET statut='paye', paye_le=? WHERE id=?",
                        (datetime.datetime.now().isoformat(timespec="seconds"), pi_id))
        elif statut in ("failed", "canceled"):
            con.execute("UPDATE ventes SET statut=? WHERE id=?", (statut, pi_id))
    if statut == "completed":
        txt = (f"💰 PAIEMENT CONFIRMÉ\nTeam Leader ou Agent : {nom}\nJoueur : {joueur}\n"
               f"Montant : {cents/100:.2f} €\nRéf : {pi_id}")
        await tg_app.bot.send_message(ADMIN_CHAT_ID, txt)
        await tg_app.bot.send_message(
            agent_id, f"✅ Paiement reçu pour {joueur} : {cents/100:.2f} €")
    elif statut in ("failed", "canceled"):
        await tg_app.bot.send_message(
            agent_id, f"❌ Paiement {statut} pour {joueur} ({cents/100:.2f} €)")
    return {"ok": True}


async def main():
    db()
    await ziina_register_webhook()
    async with tg_app:
        await tg_app.start()
        await tg_app.updater.start_polling()
        port = int(os.environ.get("PORT", 8080))
        server = uvicorn.Server(
            uvicorn.Config(api, host="0.0.0.0", port=port, log_level="info"))
        await server.serve()
        await tg_app.updater.stop()
        await tg_app.stop()


if __name__ == "__main__":
    asyncio.run(main())
  
