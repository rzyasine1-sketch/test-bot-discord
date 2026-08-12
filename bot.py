"""
bot.py — Discord Bot (discord.py 2.0+)
Version: 2.0.0
Features: Decoupled auth, slash + prefix commands, voice XP, chat gamification.
"""

import os
import time
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional, Dict, Any, List, Tuple

import discord
from discord import app_commands
from discord.ext import commands, tasks
from dotenv import load_dotenv

from database import Database

# ── Environment ──
load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")
WEBSITE_BASE_URL = os.getenv("WEBSITE_BASE_URL", "https://yourdomain.com")

# ── Logging ──
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s"
)
logger = logging.getLogger("DiscordBot")

# ── Chat Cooldown Store ──
_chat_cooldowns: Dict[int, float] = {}
CHAT_COOLDOWN_SECONDS = 60


# ═══════════════════════════════════════════════════════════
# BOT CLASS
# ═══════════════════════════════════════════════════════════

class ProfileBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True
        intents.members = True
        intents.voice_states = True
        super().__init__(command_prefix="!", intents=intents, help_command=None)

    async def setup_hook(self):
        await Database.init_db()
        if not voice_xp_task.is_running():
            voice_xp_task.start()
        if not cleanup_task.is_running():
            cleanup_task.start()
        await self.tree.sync()
        logger.info("Slash commands synced globally.")


bot = ProfileBot()


# ═══════════════════════════════════════════════════════════
# BACKGROUND TASKS
# ═══════════════════════════════════════════════════════════

@tasks.loop(minutes=1)
async def voice_xp_task():
    try:
        rewards: List[Tuple[int, int, int]] = []
        for guild in bot.guilds:
            for channel in guild.voice_channels + guild.stage_channels:
                for member in channel.members:
                    if member.bot:
                        continue
                    vs = member.voice
                    if vs is None:
                        continue
                    if vs.self_mute or vs.mute or vs.self_deaf or vs.deaf:
                        continue
                    rewards.append((member.id, 1, 15))
        if rewards:
            await Database.add_xp_coins_batch(rewards)
            logger.debug(f"Voice XP awarded to {len(rewards)} user(s).")
    except Exception as e:
        logger.error(f"voice_xp_task error: {e}")


@voice_xp_task.before_loop
async def before_voice_xp_task():
    await bot.wait_until_ready()


@tasks.loop(minutes=5)
async def cleanup_task():
    try:
        deleted = await Database.cleanup_expired_codes()
        if deleted > 0:
            logger.info(f"Cleaned up {deleted} expired auth code(s).")
    except Exception as e:
        logger.error(f"cleanup_task error: {e}")


@cleanup_task.before_loop
async def before_cleanup_task():
    await bot.wait_until_ready()


# ═══════════════════════════════════════════════════════════
# SLASH COMMANDS
# ═══════════════════════════════════════════════════════════

@bot.tree.command(name="login", description="Generate a secure login code for the web dashboard.")
@app_commands.checks.cooldown(1, 30, key=lambda i: i.user.id)
async def login_slash(interaction: discord.Interaction):
    user_id = str(interaction.user.id)
    await Database.get_or_create_user(user_id)
    login_code = await Database.generate_login_code(user_id)

    embed = discord.Embed(
        title="🔐 Secure Login Code",
        description=f"Hello **{interaction.user.display_name}**,
\nUse the code below to access your web dashboard.",
        color=discord.Color.blurple(),
        timestamp=discord.utils.utcnow()
    )
    embed.add_field(
        name="🔗 Login URL",
        value=f"[Click here to open the login page]({WEBSITE_BASE_URL}/login)
`{WEBSITE_BASE_URL}/login`",
        inline=False
    )
    embed.add_field(
        name="🔑 Your Code",
        value=f"```
{login_code}
```",
        inline=False
    )
    embed.add_field(
        name="⏰ Expiration",
        value="This code expires in **5 minutes** and is **single-use**.",
        inline=False
    )
    embed.set_footer(text="Discord Bot Dashboard • Secure Auth")

    try:
        await interaction.user.send(embed=embed)
        await interaction.response.send_message(
            "📩 Check your **DMs** for the secure login code!", ephemeral=True
        )
    except discord.Forbidden:
        await interaction.response.send_message(
            "❌ I couldn't DM you. Please enable DMs from server members.", ephemeral=True
        )


@login_slash.error
async def login_slash_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.CommandOnCooldown):
        await interaction.response.send_message(
            f"⏳ Please wait **{error.retry_after:.0f}**s before requesting another code.",
            ephemeral=True
        )
    else:
        logger.error(f"/login error: {error}")
        await interaction.response.send_message(
            "❌ An unexpected error occurred.", ephemeral=True
        )


@bot.tree.command(name="profile", description="View your profile, coins, XP, and inventory.")
async def profile_slash(interaction: discord.Interaction):
    user_id = str(interaction.user.id)
    user = await Database.get_or_create_user(user_id)
    avatar_count = await Database.get_inventory_count(user_id, "avatar")
    banner_count = await Database.get_inventory_count(user_id, "banner")
    total_items = avatar_count + banner_count

    embed = discord.Embed(
        title=f"👤 {interaction.user.display_name}'s Profile",
        description="Your dashboard stats and inventory summary.",
        color=discord.Color.green(),
        timestamp=discord.utils.utcnow()
    )
    embed.add_field(
        name="💰 Economy",
        value=f"**Coins:** `{user['coins']:,}`
**XP:** `{user['xp']:,}`",
        inline=True
    )
    embed.add_field(
        name="🎒 Inventory",
        value=f"**Avatars:** `{avatar_count}`
**Banners:** `{banner_count}`
**Total:** `{total_items}`",
        inline=True
    )
    embed.add_field(
        name="📅 Member Since",
        value=f"`{user.get('created_at', 'N/A')}`",
        inline=False
    )
    embed.add_field(
        name="⚡ Quick Actions",
        value=f"Use `/login` to access the web dashboard.
Visit: {WEBSITE_BASE_URL}",
        inline=False
    )
    embed.set_thumbnail(url=interaction.user.display_avatar.url)
    embed.set_footer(text="Discord Bot Dashboard")
    await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.tree.command(name="balance", description="Check your coin balance quickly.")
async def balance_slash(interaction: discord.Interaction):
    user_id = str(interaction.user.id)
    user = await Database.get_or_create_user(user_id)
    embed = discord.Embed(
        title="💰 Your Balance",
        description=f"**{interaction.user.display_name}** currently has:

🪙 **{user['coins']:,}** coins",
        color=discord.Color.gold(),
        timestamp=discord.utils.utcnow()
    )
    embed.set_footer(text="Use /daily to claim free coins!")
    await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.tree.command(name="inventory", description="List your purchased avatars and banners.")
@app_commands.describe(item_type="Filter by item type", page="Page number")
@app_commands.choices(item_type=[
    app_commands.Choice(name="All", value="all"),
    app_commands.Choice(name="Avatars", value="avatar"),
    app_commands.Choice(name="Banners", value="banner"),
])
async def inventory_slash(
    interaction: discord.Interaction,
    item_type: app_commands.Choice[str] = None,
    page: int = 1
):
    user_id = str(interaction.user.id)
    await Database.get_or_create_user(user_id)
    filter_type = item_type.value if item_type else "all"
    inventory = await Database.get_user_inventory(user_id)

    if filter_type != "all":
        inventory = [it for it in inventory if it.get("item_type") == filter_type]

    if not inventory:
        await interaction.response.send_message(
            "🎒 Your inventory is empty! Visit the shop to buy some items.", ephemeral=True
        )
        return

    per_page = 5
    total_pages = max(1, (len(inventory) + per_page - 1) // per_page)
    page = max(1, min(page, total_pages))
    start = (page - 1) * per_page
    end = start + per_page
    page_items = inventory[start:end]

    embed = discord.Embed(
        title=f"🎒 {interaction.user.display_name}'s Inventory",
        description=f"Showing {len(page_items)} of {len(inventory)} items (Page {page}/{total_pages})",
        color=discord.Color.purple(),
        timestamp=discord.utils.utcnow()
    )
    for idx, item in enumerate(page_items, start=start + 1):
        name = item.get("avatar_id") or item.get("local_file_path") or item.get("item_type", "Item")
        embed.add_field(
            name=f"#{idx} {item.get('item_type','Item').capitalize()}",
            value=f"ID: `{name}`
Purchased: `{item.get('purchased_at','N/A')}`",
            inline=True
        )
    embed.set_footer(text=f"Filter: {filter_type.capitalize()} | Use /inventory page:2")
    await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.tree.command(name="daily", description="Claim your daily reward! Resets every 24 hours.")
async def daily_slash(interaction: discord.Interaction):
    user_id = interaction.user.id
    success, new_coins, remaining = await Database.claim_daily(user_id)
    if success:
        embed = discord.Embed(
            title="🎁 Daily Reward Claimed!",
            description=f"**{interaction.user.display_name}** claimed their daily reward!",
            color=discord.Color.green(),
            timestamp=discord.utils.utcnow()
        )
        embed.add_field(name="🪙 Coins", value=f"+{new_coins:,}", inline=True)
        embed.add_field(name="⭐ XP", value=f"+{Database.DAILY_XP_REWARD}", inline=True)
        embed.set_footer(text="Come back tomorrow for another reward!")
        await interaction.response.send_message(embed=embed)
    else:
        hours, remainder = divmod(int(remaining.total_seconds()), 3600)
        minutes, _ = divmod(remainder, 60)
        time_str = f"{hours}h {minutes}m" if hours > 0 else f"{minutes}m"
        await interaction.response.send_message(
            f"⏳ You already claimed your daily reward!
Come back in **{time_str}**.",
            ephemeral=True
        )


@bot.tree.command(name="gift", description="Gift coins to another user.")
@app_commands.describe(user="The user to gift coins to", amount="Amount of coins to gift")
@app_commands.checks.cooldown(1, 10, key=lambda i: i.user.id)
async def gift_slash(interaction: discord.Interaction, user: discord.User, amount: int):
    if amount <= 0:
        await interaction.response.send_message("❌ Amount must be positive.", ephemeral=True)
        return
    if user.id == interaction.user.id:
        await interaction.response.send_message("❌ You can't gift coins to yourself!", ephemeral=True)
        return

    sender_id = str(interaction.user.id)
    receiver_id = str(user.id)

    sender = await Database.get_or_create_user(sender_id)
    if sender["coins"] < amount:
        await interaction.response.send_message(
            f"❌ Insufficient funds! You have {sender['coins']:,} coins.", ephemeral=True
        )
        return

    # Deduct sender
    ok1 = await Database.update_user_coins(sender_id, -amount)
    if not ok1:
        await interaction.response.send_message("❌ Transaction failed.", ephemeral=True)
        return

    # Credit receiver
    await Database.get_or_create_user(receiver_id)
    ok2 = await Database.update_user_coins(receiver_id, amount)
    if not ok2:
        # Rollback
        await Database.update_user_coins(sender_id, amount)
        await interaction.response.send_message(
            "❌ Transfer failed. Coins have been refunded.", ephemeral=True
        )
        return

    embed = discord.Embed(
        title="🎁 Gift Sent!",
        description=f"**{interaction.user.display_name}** gifted **{amount:,}** coins to **{user.display_name}**!",
        color=discord.Color.pink(),
        timestamp=discord.utils.utcnow()
    )
    await interaction.response.send_message(embed=embed)


@gift_slash.error
async def gift_slash_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.CommandOnCooldown):
        await interaction.response.send_message(
            f"⏳ Wait **{error.retry_after:.0f}**s between gifts.", ephemeral=True
        )
    else:
        await interaction.response.send_message("❌ Gift failed.", ephemeral=True)


@bot.tree.command(name="shop", description="Get a link to the web shop.")
async def shop_slash(interaction: discord.Interaction):
    embed = discord.Embed(
        title="🛒 Image Marketplace",
        description=f"Browse and buy avatars & banners!

[Open Shop]({WEBSITE_BASE_URL}/shop)",
        color=discord.Color.blue(),
        timestamp=discord.utils.utcnow()
    )
    embed.add_field(name="💡 Tip", value="Use `/balance` to check your coins before shopping!", inline=False)
    await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.tree.command(name="dashboard", description="Get a quick link to the web dashboard.")
async def dashboard_slash(interaction: discord.Interaction):
    embed = discord.Embed(
        title="🌐 Web Dashboard",
        description=f"Access your full dashboard here:
{WEBSITE_BASE_URL}",
        color=discord.Color.blue()
    )
    embed.add_field(name="📝 Note", value="Use `/login` if you need a fresh authentication code.", inline=False)
    await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.tree.command(name="stats", description="View XP and Coins stats for yourself or another member.")
@app_commands.describe(member="The member whose stats you want to view.")
async def stats_slash(interaction: discord.Interaction, member: Optional[discord.Member] = None):
    target = member or interaction.user
    stats = await Database.get_user_stats(target.id)
    embed = discord.Embed(
        title=f"📊 {target.display_name}'s Stats",
        color=discord.Color.gold()
    )
    embed.set_thumbnail(url=target.display_avatar.url)
    embed.add_field(name="⭐ Total XP", value=f"**{stats['xp']:,}**", inline=True)
    embed.add_field(name="🪙 Total Coins", value=f"**{stats['coins']:,}**", inline=True)
    embed.set_footer(
        text=f"Requested by {interaction.user.display_name}",
        icon_url=interaction.user.display_avatar.url
    )
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="leaderboard", description="View the top 10 users by XP and Coins.")
async def leaderboard_slash(interaction: discord.Interaction):
    data = await Database.get_leaderboard(limit=10)
    embed = discord.Embed(title="🏆 Server Leaderboard", color=discord.Color.purple())

    medals = ["🥇", "🥈", "🥉"]
    if data["xp"]:
        lines = []
        for idx, entry in enumerate(data["xp"], start=1):
            prefix = medals[idx - 1] if idx <= 3 else f"`#{idx}`"
            lines.append(f"{prefix} <@{entry['user_id']}> — **{entry['xp']:,}** XP")
        embed.add_field(name="⭐ Top XP", value="
".join(lines), inline=False)
    else:
        embed.add_field(name="⭐ Top XP", value="No data recorded yet.", inline=False)

    if data["coins"]:
        lines = []
        for idx, entry in enumerate(data["coins"], start=1):
            prefix = medals[idx - 1] if idx <= 3 else f"`#{idx}`"
            lines.append(f"{prefix} <@{entry['user_id']}> — **{entry['coins']:,}** Coins")
        embed.add_field(name="🪙 Top Coins", value="
".join(lines), inline=False)
    else:
        embed.add_field(name="🪙 Top Coins", value="No data recorded yet.", inline=False)

    embed.set_footer(
        text=f"Requested by {interaction.user.display_name}",
        icon_url=interaction.user.display_avatar.url
    )
    await interaction.response.send_message(embed=embed)


# ═══════════════════════════════════════════════════════════
# PREFIX COMMANDS (Legacy Compatibility)
# ═══════════════════════════════════════════════════════════

@bot.command(name="profile")
async def profile_prefix(ctx: commands.Context):
    """Generates a login code and DMs it to the user."""
    user_id = str(ctx.author.id)
    await Database.get_or_create_user(user_id)
    login_code = await Database.generate_login_code(user_id)

    embed = discord.Embed(
        title="🔐 Secure Login Code",
        description=f"Hello **{ctx.author.display_name}**,
\nUse the code below to access your web dashboard.",
        color=discord.Color.blurple(),
        timestamp=discord.utils.utcnow()
    )
    embed.add_field(
        name="🔗 Login URL",
        value=f"[Click here to open the login page]({WEBSITE_BASE_URL}/login)
`{WEBSITE_BASE_URL}/login`",
        inline=False
    )
    embed.add_field(
        name="🔑 Your Code",
        value=f"```
{login_code}
```",
        inline=False
    )
    embed.add_field(
        name="⏰ Expiration",
        value="This code expires in **5 minutes** and is **single-use**.",
        inline=False
    )
    embed.set_footer(text="Discord Bot Dashboard • Secure Auth")

    try:
        await ctx.author.send(embed=embed)
        await ctx.reply("📩 Check your **DMs** for the secure login code!", mention_author=True)
    except discord.Forbidden:
        await ctx.reply("❌ I couldn't DM you. Please enable DMs from server members.", mention_author=True)


@bot.command(name="stats")
async def stats_prefix(ctx: commands.Context, member: Optional[discord.Member] = None):
    target = member or ctx.author
    stats = await Database.get_user_stats(target.id)
    embed = discord.Embed(
        title=f"📊 {target.display_name}'s Stats",
        color=discord.Color.gold()
    )
    embed.set_thumbnail(url=target.display_avatar.url)
    embed.add_field(name="⭐ Total XP", value=f"**{stats['xp']:,}**", inline=True)
    embed.add_field(name="🪙 Total Coins", value=f"**{stats['coins']:,}**", inline=True)
    embed.set_footer(
        text=f"Requested by {ctx.author.display_name}",
        icon_url=ctx.author.display_avatar.url
    )
    await ctx.reply(embed=embed)


@bot.command(name="leaderboard")
async def leaderboard_prefix(ctx: commands.Context):
    data = await Database.get_leaderboard(limit=10)
    embed = discord.Embed(title="🏆 Server Leaderboard", color=discord.Color.purple())
    medals = ["🥇", "🥈", "🥉"]
    if data["xp"]:
        lines = []
        for idx, entry in enumerate(data["xp"], start=1):
            prefix = medals[idx - 1] if idx <= 3 else f"`#{idx}`"
            lines.append(f"{prefix} <@{entry['user_id']}> — **{entry['xp']:,}** XP")
        embed.add_field(name="⭐ Top XP", value="
".join(lines), inline=False)
    else:
        embed.add_field(name="⭐ Top XP", value="No data recorded yet.", inline=False)
    if data["coins"]:
        lines = []
        for idx, entry in enumerate(data["coins"], start=1):
            prefix = medals[idx - 1] if idx <= 3 else f"`#{idx}`"
            lines.append(f"{prefix} <@{entry['user_id']}> — **{entry['coins']:,}** Coins")
        embed.add_field(name="🪙 Top Coins", value="
".join(lines), inline=False)
    else:
        embed.add_field(name="🪙 Top Coins", value="No data recorded yet.", inline=False)
    embed.set_footer(
        text=f"Requested by {ctx.author.display_name}",
        icon_url=ctx.author.display_avatar.url
    )
    await ctx.reply(embed=embed)


@bot.command(name="daily")
async def daily_prefix(ctx: commands.Context):
    success, new_coins, remaining = await Database.claim_daily(ctx.author.id)
    if success:
        embed = discord.Embed(
            title="🎁 Daily Reward Claimed!",
            description=f"You successfully claimed your daily reward!

💰 **New Balance:** **{new_coins:,}** Coins",
            color=discord.Color.green()
        )
    else:
        hours, remainder = divmod(int(remaining.total_seconds()), 3600)
        minutes, _ = divmod(remainder, 60)
        time_str = f"{hours}h {minutes}m" if hours > 0 else f"{minutes}m"
        embed = discord.Embed(
            title="⏰ Daily Reward Cooldown",
            description=f"You have already claimed your daily reward today!

Please wait **{time_str}** before claiming again.",
            color=discord.Color.red()
        )
    embed.set_footer(
        text=f"Requested by {ctx.author.display_name}",
        icon_url=ctx.author.display_avatar.url
    )
    await ctx.reply(embed=embed)


@bot.command(name="inventory")
async def inventory_prefix(ctx: commands.Context):
    items = await Database.get_user_inventory_ids(ctx.author.id)
    embed = discord.Embed(
        title=f"🎒 {ctx.author.display_name}'s Inventory",
        color=discord.Color.blue()
    )
    if not items:
        embed.description = "You haven't bought any avatars from the Web Dashboard yet!"
    else:
        item_list = "
".join([f"• `{aid}`" for aid in items])
        embed.description = f"**Owned Custom Avatars:**

{item_list}"
    embed.set_footer(
        text=f"Requested by {ctx.author.display_name}",
        icon_url=ctx.author.display_avatar.url
    )
    await ctx.reply(embed=embed)


# ═══════════════════════════════════════════════════════════
# EVENTS
# ═══════════════════════════════════════════════════════════

@bot.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return

    ctx = await bot.get_context(message)
    if ctx.valid:
        await bot.process_commands(message)
        return

    # Chat gamification (+1 XP, +5 Coins per 60s)
    user_id = message.author.id
    now = time.time()
    last_earned = _chat_cooldowns.get(user_id, 0)
    if now - last_earned >= CHAT_COOLDOWN_SECONDS:
        _chat_cooldowns[user_id] = now
        await Database.add_xp_coins(user_id, xp=1, coins=5)

    await bot.process_commands(message)


@bot.event
async def on_ready():
    logger.info(f"Bot online as {bot.user} (ID: {bot.user.id})")
    logger.info(f"Connected to {len(bot.guilds)} guild(s)")


# ═══════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════

if __name__ == "__main__":
    if not TOKEN or TOKEN == "your_discord_bot_token_here":
        logger.error("DISCORD_TOKEN is missing or not set in .env!")
    else:
        bot.run(TOKEN)
