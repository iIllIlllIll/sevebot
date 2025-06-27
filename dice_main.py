import os
import random, asyncio
import sqlite3
import json
from datetime import datetime, timedelta
from collections import defaultdict

import discord
from discord.ext import commands
from discord import app_commands
from discord.ui import View, Button, Select, Modal, TextInput

# ─── 1) Config & Constants ─────────────────────────────────────
with open("keys.json", "r", encoding="utf-8") as f:
    cfg = json.load(f)
DISCORD_TOKEN = cfg["dice_bot_token"]
GUILD_ID      = cfg.get("guild_id")
MENTION_ROLE_ID = None
CONSOLE_CHANNEL_ID = cfg.get("console_channel_id")
COMMAND_CHANNEL_ID = cfg.get("command_channel_id")
test_guild    = discord.Object(id=GUILD_ID) if GUILD_ID else None

NUMBERS_FOLDER = os.path.join(os.getcwd(), "numbers")
MIN_PLAYERS = 2
MAX_PLAYERS = 10

game_counter = 0

# ─── 2) Database setup ─────────────────────────────────────────
conn   = sqlite3.connect("dice_game.db")
cursor = conn.cursor()
cursor.execute("""
CREATE TABLE IF NOT EXISTS users (
    user_id TEXT PRIMARY KEY,
    chips   INTEGER NOT NULL DEFAULT 1000
)
""")
conn.commit()

def get_user_chips(uid: int) -> int:
    cursor.execute("SELECT chips FROM users WHERE user_id = ?", (str(uid),))
    row = cursor.fetchone()
    if row:
        return row[0]
    cursor.execute("INSERT INTO users(user_id) VALUES(?)", (str(uid),))
    conn.commit()
    return 1000

def update_user_chips(uid: int, new_chips: int):
    cursor.execute("UPDATE users SET chips = ? WHERE user_id = ?", (new_chips, str(uid)))
    conn.commit()

# ─── 3) Bot setup ───────────────────────────────────────────────
intents = discord.Intents.default()
intents.message_content = True
intents.members = True

bot  = commands.Bot(command_prefix="!", intents=intents)
tree = bot.tree

console_channel = None

# Active games per channel
active_games = {}  # channel_id -> game_data

def in_command_channel():
    def predicate(inter: discord.Interaction) -> bool:
        # DM, 콘솔 로그 채널 등은 제외
        if inter.channel.id != COMMAND_CHANNEL_ID:
            raise app_commands.CheckFailure(
                f"❌ 이 명령어는 <#{COMMAND_CHANNEL_ID}> 채널에서만 사용할 수 있습니다."
            )
        return True
    return app_commands.check(predicate)

# ─── 4) Game Data Structure ────────────────────────────────────
class DiceGame:
    def __init__(self, channel, bet: int, max_players: int):
        self.channel = channel
        self.bet = bet
        self.max_players = max_players
        self.tag = None            # ex: "#0001"
        self.participants = []         # list of user IDs
        self.initial_rolls = {}        # uid -> roll1
        self.second_rolls = {}         # uid -> roll2
        self.folded = set()            # uids who folded
        self.responded = set()         # uids who made a choice
        self.join_msg = None           # channel message with join button

# ─── 5) Views ───────────────────────────────────────────────────
class JoinView(View):
    def __init__(self, game: DiceGame):
        super().__init__(timeout=None)
        self.game = game

    @discord.ui.button(label="참가", style=discord.ButtonStyle.primary)
    async def join(self, interaction: discord.Interaction, button: Button):
        uid = interaction.user.id
        if uid in self.game.participants:
            return await interaction.response.send_message("이미 참가하셨습니다!", ephemeral=True)
        if len(self.game.participants) >= self.game.max_players:
            return await interaction.response.send_message("참가 인원이 모두 찼습니다.", ephemeral=True)

        # Deduct bet
        chips = get_user_chips(uid)
        if chips < self.game.bet:
            return await interaction.response.send_message("🛑 잔액이 부족합니다.", ephemeral=True)
        update_user_chips(uid, chips - self.game.bet)

        self.game.participants.append(uid)
        await interaction.response.send_message(f"✅ 참가 완료! …", ephemeral=True)
        if console_channel:
            await console_channel.send(
                f"[{self.game.tag}] 🎉 <@{uid}> 참가 ({len(self.game.participants)}/{self.game.max_players})"
            )
        await self._update_join_embed(interaction)

        if len(self.game.participants) == self.game.max_players:
            self.start_game()

    @discord.ui.button(label="취소", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: Button):
        uid = interaction.user.id
        if uid not in self.game.participants:
            return await interaction.response.send_message("아직 참가하지 않으셨습니다.", ephemeral=True)

        # 주최자가 취소하면 게임 전체 취소
        if uid == self.game.host:
            # 주최자 베팅 환급
            chips = get_user_chips(uid)
            update_user_chips(uid, chips + self.game.bet)
            # 버튼 비활성화 후 메시지 수정
            for item in self.children:
                item.disabled = True
            await interaction.response.edit_message(
                embed=discord.Embed(
                    title=f"{self.game.tag} 게임 취소됨",
                    description="주최자가 게임을 취소했습니다.",
                    color=0xff0000
                ),
                view=self
            )
            # 게임 데이터 삭제
            del active_games[self.game.channel.id]
            if console_channel:
                await console_channel.send(f"[{self.game.tag}] ❌ 주최자 <@{uid}> 게임 취소")
            return

        # 일반 참가자 취소: 전액 환급 후 명단에서 제거
        chips = get_user_chips(uid)
        update_user_chips(uid, chips + self.game.bet)
        self.game.participants.remove(uid)
        await interaction.response.send_message(
            f"❎ 참가 취소! 베팅액 `{self.game.bet}`칩이 환급되었습니다.",
            ephemeral=True
        )
        if console_channel:
            await console_channel.send(f"[{self.game.tag}] 🚪 <@{uid}> 참가 취소")
        await self._update_join_embed(interaction)

    async def _update_join_embed(self, interaction: discord.Interaction):
        # 참가자 리스트 & 카운트 갱신
        names = [
            interaction.guild.get_member(u).display_name
            for u in self.game.participants
        ]
        embed = discord.Embed(
            title=f"🎲 Dice Game 모집 중 {self.game.tag}",
            color=0x00ff00
        )
        embed.add_field(name="👑 주최자", value=f"<@{self.game.host}>", inline=True)
        embed.add_field(name="💰 베팅액", value=f"{self.game.bet}칩", inline=True)
        embed.add_field(name="👤 참가자", value=", ".join(names) or "없음", inline=True)
        embed.add_field(
            name="👥 목표 인원",
            value=f"{len(self.game.participants)}/{self.game.max_players}명",
            inline=True
        )
        await self.game.join_msg.edit(embed=embed, view=self)

    def start_game(self):
        # Disable join button
        for item in self.children:
            item.disabled = True
        # Kick off first roll
        bot.loop.create_task(begin_first_roll(self.game))
        bot.loop.create_task(first_roll_timeout(self.game))

class ChoiceView(View):
    def __init__(self, game: DiceGame, uid: int):
        super().__init__(timeout=None)
        self.game = game
        self.uid = uid

    @discord.ui.button(label="폴드", style=discord.ButtonStyle.danger)
    async def fold(self, interaction: discord.Interaction, button: Button):
        if interaction.user.id != self.uid:
            return await interaction.response.send_message("❌ 당신의 게임이 아닙니다.", ephemeral=True)
        if self.uid in self.game.responded:
            return await interaction.response.send_message("이미 선택하셨습니다.", ephemeral=True)

        self.game.folded.add(self.uid)
        self.game.responded.add(self.uid)
        # Refund 50%
        refund = self.game.bet // 2
        chips = get_user_chips(self.uid)
        update_user_chips(self.uid, chips + refund)
        embed = discord.Embed(
            title="💤 Fold",
            description=f"폴드 하셨습니다. `{refund}`칩 환급되었습니다.",
            color=0xE67E22
        )
        await interaction.response.edit_message(embed=embed, view=None)
        # 콘솔에 폴드 로그
        if console_channel:
            await console_channel.send(f"[{self.game.tag}] 💤 <@{self.uid}> 폴드")

        # Check if all responded
        if len(self.game.responded) == len(self.game.participants):
            remaining = [u for u in self.game.participants if u not in self.game.folded]
            # 남은 인원이 0명 혹은 1명일 때 즉시 종료
            if len(remaining) <= 1:
                bot.loop.create_task(resolve_immediate(self.game))
            else:
                bot.loop.create_task(begin_second_roll(self.game))

    @discord.ui.button(label="계속", style=discord.ButtonStyle.success)
    async def cont(self, interaction: discord.Interaction, button: Button):
        if interaction.user.id != self.uid:
            return await interaction.response.send_message("❌ 당신의 게임이 아닙니다.", ephemeral=True)
        if self.uid in self.game.responded:
            return await interaction.response.send_message("이미 선택하셨습니다.", ephemeral=True)

        self.game.responded.add(self.uid)
        embed = discord.Embed(
            title="▶️ Continue",
            description="두 번째 주사위를 굴리기 전까지 대기중입니다…",
            color=0x2ECC71
        )
        await interaction.response.edit_message(embed=embed, view=None)
        # 콘솔에 계속 진행 로그
        if console_channel:
            await console_channel.send(f"[{self.game.tag}] ▶️ <@{self.uid}> 계속 진행")

        # 모두 응답했으면
        if len(self.game.responded) == len(self.game.participants):
            remaining = [u for u in self.game.participants if u not in self.game.folded]
            if len(remaining) == 1:
                bot.loop.create_task(resolve_immediate(self.game))
            else:
                bot.loop.create_task(begin_second_roll(self.game))

# ─── 6) Game Flow ──────────────────────────────────────────────
async def begin_first_roll(game: DiceGame):
    # Notify channel
    embed = discord.Embed(
        title="🎲 첫 번째 주사위 굴리는 중…",
        color=0x3498DB
    )
    await game.join_msg.channel.send(embed=embed)
    # Roll for each participant
    for uid in game.participants:
        roll = random.randint(1, 20)
        game.initial_rolls[uid] = roll
        # DM with image if exists
        user = await bot.fetch_user(uid)
        dm = await user.create_dm()
        path = os.path.join(NUMBERS_FOLDER, f"{roll}.png")
        if os.path.isfile(path):
            await dm.send(file=discord.File(path))
        else:
            await dm.send(f"🎲 당신의 첫 번째 주사위: **{roll}**")
        view = ChoiceView(game, uid)
        view = ChoiceView(game, uid)
        embed_sel = discord.Embed(
            title="🎲 선택",
            description="‘폴드’ 또는 ‘계속’ 버튼을 눌러주세요.",
            color=0xF1C40F
        )
        await dm.send(embed=embed_sel, view=view)
        # 콘솔에 첫 주사위 결과 로그
        if console_channel:
            await console_channel.send(f"[{game.tag}] 🎲 <@{uid}> 첫 주사위: {roll}")

async def resolve_immediate(game: DiceGame):
    # 남은 플레이어(폴드하지 않은)가 1명인 즉시 승리 처리
    remaining = [u for u in game.participants if u not in game.folded]
    if not remaining:
        # 모두 폴드한 경우
        await game.channel.send("모두 폴드하여 우승자가 없습니다.")
        del active_games[game.channel.id]
        return

    winner = remaining[0]

    # 판돈 계산: 총 베팅액 – 환급액
    total_bets   = game.bet * len(game.participants)
    total_refund = (game.bet // 2) * len(game.folded)
    pot          = total_bets - total_refund

    # 승자에게 전부 지급
    reward = pot
    chips = get_user_chips(winner)
    update_user_chips(winner, chips + reward)

    # 결과 공개
    embed = discord.Embed(title="🎲 Dice Game 결과 (즉시 종료)", color=0x00ff00)
    lines = []
    for uid in game.participants:
        init = game.initial_rolls.get(uid, None)
        status = (
            "폴드" if uid in game.folded
            else f"{init}"
        )
        mark = "🏆" if uid == winner else ""
        member = game.join_msg.guild.get_member(uid)
        name = member.display_name if member else str(uid)
        lines.append(f"{mark} {name}: {status}")
    embed.description = "\n".join(lines)
    embed.add_field(
        name="우승자",
        value=f"{game.join_msg.guild.get_member(winner).display_name}님\n획득 칩: {reward}",
        inline=False
    )

    await game.channel.send(embed=embed)

    # 게임 정리
    del active_games[game.channel.id]

async def begin_second_roll(game: DiceGame):
    # Roll second for those who did not fold
    # 알림: 두 번째 주사위 단계 시작
    embed = discord.Embed(
        title="🎲 두 번째 주사위 굴리는 중...",
        color=0x3498DB
    )
    await game.join_msg.channel.send(embed=embed)
    if console_channel:
        await console_channel.send(f"[{game.tag}] 🎲 두 번째 주사위 시작")
    cont = [u for u in game.participants if u not in game.folded]
    for uid in cont:
        roll = random.randint(1, 20)
        game.second_rolls[uid] = roll
        game_sum = game.initial_rolls[uid] + roll
        # DM second roll
        user = await bot.fetch_user(uid)
        dm = await user.create_dm()
        path = os.path.join(NUMBERS_FOLDER, f"{roll}.png")
        if os.path.isfile(path):
            await dm.send(file=discord.File(path))
        else:
            await dm.send(f"🎲 두 번째 주사위: **{roll}**")
        # 합계 알림도 Embed로
        e2 = discord.Embed(
            title="🏁 합계",
            description=f"첫 번째 + 두 번째 주사위 합: **{game_sum}**",
            color=0x9B59B6
        )
        await dm.send(embed=e2)
        # 콘솔에 두 번째 주사위 결과 로그
        if console_channel:
            await console_channel.send(f"[{game.tag}] 🎲 <@{uid}> 두 번째 주사위: {roll} (합계 {game_sum})")

    # Compute pot: sum of all bets minus refunds
    total_bets = game.bet * len(game.participants)
    total_refund = (game.bet // 2) * len(game.folded)
    pot = total_bets - total_refund

    # Determine winner(s)
    sums = {}
    for uid in cont:
        sums[uid] = game.initial_rolls[uid] + game.second_rolls[uid]
    if sums:
        max_sum = max(sums.values())
        winners = [u for u, s in sums.items() if s == max_sum]
    else:
        winners = []

    reward = pot // len(winners) if winners else 0
    # Payout
    for uid in winners:
        chips = get_user_chips(uid)
        update_user_chips(uid, chips + reward)

    # Public reveal
    embed = discord.Embed(title=f"🎲 Dice Game 결과 {game.tag}", color=0x00ff00)
    lines = []
    for uid in game.participants:
        init = game.initial_rolls[uid]
        sec  = game.second_rolls.get(uid, None)
        status = "폴드" if uid in game.folded else f"{init} + {sec} = **{init+sec}**"
        mark = "🏆" if uid in winners else ""
        member = game.join_msg.guild.get_member(uid)
        name = member.display_name if member else str(uid)
        lines.append(f"{mark} {name}: {status}")
    embed.description = "\n".join(lines)
    if winners:
        win_names = [game.join_msg.guild.get_member(u).display_name for u in winners]
        embed.add_field(
            name="🎖️우승자",
            value=", ".join(win_names) + f"\n획득 칩: {reward}💰",
            inline=False
        )
    else:
        embed.add_field(name="결과", value="모두 폴드하여 우승자가 없습니다.", inline=False)

    await game.channel.send(embed=embed)
    # 🛑 모집 뷰(참가/취소 버튼) 제거
    try:
        await game.join_msg.edit(view=None)
    except:
        pass
    if console_channel:
        await console_channel.send(embed=embed)
    # Clean up
    del active_games[game.channel.id]

# ─── 7) /dice 명령어 ───────────────────────────────────────────
@tree.command(
    name="dice",
    description="버튼으로 2~10명 모집 후 주사위 게임 시작",
    guild=test_guild
)
@in_command_channel()
@app_commands.describe(
    bet="베팅할 칩 수",
    players="참가 인원 수 (2~10)"
)
async def dice_cmd(inter: discord.Interaction, bet: int, players: int):
    if players < MIN_PLAYERS or players > MAX_PLAYERS:
        return await inter.response.send_message(
            f"❌ 참가 인원은 {MIN_PLAYERS}명 이상, {MAX_PLAYERS}명 이하만 가능합니다.",
            ephemeral=True
        )
    if bet <= 0:
        return await inter.response.send_message("❌ 올바른 베팅 금액을 입력하세요.", ephemeral=True)

    # Deduct nothing till join; chips deducted on join
    # Create game
    global game_counter
    game_counter += 1
    game = DiceGame(inter.channel, bet, players)
    # 주최자 자동 참가
    host_id = inter.user.id
    game.host = host_id
    # 주최자 베팅 금액 즉시 차감
    host_chips = get_user_chips(host_id)
    update_user_chips(host_id, host_chips - bet)
    game.participants.append(host_id)
    game.tag = f"#{game_counter:04d}"
    active_games[inter.channel.id] = game

    # 역할 멘션이 필요하면 content에 추가
    role_mention = f"<@&{MENTION_ROLE_ID}>" if MENTION_ROLE_ID else None
    embed = discord.Embed(
        title=f"🎲 Dice Game 모집 중 {game.tag}",
        description="새 게임이 시작됩니다!",
        color=0x00ff00
    )
    # 주최자 • 참가자 현황 표시
    embed.add_field(name="👑 주최자",    value=f"<@{host_id}>",               inline=True)
    embed.add_field(name="💰 베팅액",    value=f"{bet}칩",                   inline=True)
    embed.add_field(name="👤 참가자",    value="없음" if len(game.participants)==0 else f"<@{host_id}>", inline=True)
    embed.add_field(name="👥 목표 인원", value=f"1/{players}명",               inline=True)

    # 콘솔에 게임 시작 로그
    if console_channel:
        log = discord.Embed(
            title=f"[{game.tag}] 게임 시작",
            description=(
                f"👑 주최자: <@{host_id}>\n"
                f"💰 베팅액: {bet}칩\n"
                f"👤 참가: <@{host_id}>\n"
                f"👥 목표인원: {players}명"
            ),
            color=0x3498DB
        )
        await console_channel.send(embed=log)

    view = JoinView(game)
    if role_mention:
        msg = await inter.response.send_message(content=role_mention, embed=embed, view=view)
    else:
        msg = await inter.response.send_message(embed=embed, view=view)
    game.join_msg = await inter.original_response()


@tree.command(
    name="quit",
    description="게임에서 중도 포기 (환급 없이 탈락)",
    guild=test_guild
)
@in_command_channel()
async def quit_cmd(inter: discord.Interaction):
    channel_id = inter.channel.id
    if channel_id not in active_games:
        return await inter.response.send_message(
            "❌ 진행 중인 게임이 없습니다.", ephemeral=True
        )

    game = active_games[channel_id]
    uid  = inter.user.id
    if uid not in game.participants or uid in game.folded:
        return await inter.response.send_message(
            "❌ 당신은 참가 중이 아닙니다.", ephemeral=True
        )

    # 중도 포기: 환급 없이 탈락 처리
    game.folded.add(uid)
    game.responded.add(uid)
    await inter.response.send_message(
        "❌ 중도 포기하셨습니다. 환급 없이 탈락 처리됩니다.", 
        ephemeral=True
    )

    # 모두 응답(또는 포기)했으면 다음 단계로 진행
    if len(game.responded) == len(game.participants):
        remaining = [
            u for u in game.participants 
            if u not in game.folded
        ]
        if len(remaining) == 1:
            bot.loop.create_task(resolve_immediate(game))
        else:
            bot.loop.create_task(begin_second_roll(game))



async def first_roll_timeout(game: DiceGame):
    await asyncio.sleep(300)  # 5분
    # 이미 해제됐거나 끝난 게임이면 패스
    if game.channel.id not in active_games:
        return
    # 응답 안 한 사람들은 전부 탈락
    to_remove = [u for u in game.participants if u not in game.responded]
    for uid in to_remove:
        game.folded.add(uid)
        game.responded.add(uid)
    # 안내 메시지
    await game.channel.send(
        f"⏰ 5분 경과로 응답 없는 유저 {len(to_remove)}명 탈락 처리되었습니다."
    )
    # 진행
    remaining = [u for u in game.participants if u not in game.folded]
    if len(remaining) == 1:
        await resolve_immediate(game)
    else:
        await begin_second_roll(game)



# ─── 8) Bot start ──────────────────────────────────────────────
@bot.event
async def on_ready():
    print(f"✅ Logged in as {bot.user}")
    global console_channel
    console_channel = bot.get_channel(CONSOLE_CHANNEL_ID)
    await tree.sync(guild=test_guild)

bot.run(DISCORD_TOKEN)
