import os
import json
import random
import sqlite3
from collections import defaultdict

import discord
from discord.ext import commands
from discord import app_commands
from discord.ui import View, Button, Select, Modal, TextInput

# ─── 1) Load config ─────────────────────────────────────────────
with open("keys.json", "r") as f:
    config = json.load(f)

DISCORD_TOKEN = config["discord_bot_token"]
GUILD_ID      = config.get("guild_id", 1263856763762118727)
test_guild    = discord.Object(id=GUILD_ID)

# ─── 2) SQLite setup ────────────────────────────────────────────
conn   = sqlite3.connect("mines_game.db")
cursor = conn.cursor()
cursor.execute("""
CREATE TABLE IF NOT EXISTS users (
    user_id       TEXT PRIMARY KEY,
    chips         INTEGER NOT NULL DEFAULT 1000,
    last_bet      INTEGER DEFAULT 100,
    wins          INTEGER DEFAULT 0,
    losses        INTEGER DEFAULT 0,
    default_size  INTEGER DEFAULT 3,
    default_mines INTEGER DEFAULT 3
)
""")
for col, default in (
    ("wins",0), ("losses",0),
    ("default_size",3), ("default_mines",3)
):
    try:
        cursor.execute(f"ALTER TABLE users ADD COLUMN {col} INTEGER DEFAULT {default}")
    except sqlite3.OperationalError:
        pass
conn.commit()

# ─── 3) Persistence helpers ─────────────────────────────────────
def get_user_data(uid):
    cursor.execute("SELECT chips,last_bet FROM users WHERE user_id=?", (str(uid),))
    row = cursor.fetchone()
    if row: return row
    cursor.execute("INSERT INTO users(user_id) VALUES(?)", (str(uid),))
    conn.commit()
    return (1000,100)

def update_user_data(uid, chips=None, last_bet=None):
    if chips is not None:
        cursor.execute("UPDATE users SET chips=? WHERE user_id=?", (chips, str(uid)))
    if last_bet is not None:
        cursor.execute("UPDATE users SET last_bet=? WHERE user_id=?", (last_bet, str(uid)))
    conn.commit()

def get_user_settings(uid):
    get_user_data(uid)
    cursor.execute("SELECT default_size,default_mines FROM users WHERE user_id=?", (str(uid),))
    s, m = cursor.fetchone()
    return {"size":s,"mines":m}

def update_user_settings(uid, size=None, mines=None):
    if size is not None:
        cursor.execute("UPDATE users SET default_size=? WHERE user_id=?", (size, str(uid)))
    if mines is not None:
        cursor.execute("UPDATE users SET default_mines=? WHERE user_id=?", (mines, str(uid)))
    conn.commit()

def get_user_stats(uid):
    cursor.execute("SELECT wins,losses FROM users WHERE user_id=?", (str(uid),))
    row = cursor.fetchone()
    return row if row else (0,0)

def add_win(uid):
    cursor.execute("UPDATE users SET wins=wins+1 WHERE user_id=?", (str(uid),))
    conn.commit()

def add_loss(uid):
    cursor.execute("UPDATE users SET losses=losses+1 WHERE user_id=?", (str(uid),))
    conn.commit()

# ─── 4) Multiplier ───────────────────────────────────────────────
def calculate_stake_multiplier(d,m,k):
    if k==0: return 1.00
    p=1.0
    for i in range(k):
        p *= (d-m-i)/(d-i)
    return round(1/p,2)

# ─── 5) Bot setup ────────────────────────────────────────────────
intents = discord.Intents.default()
intents.message_content = True
bot  = commands.Bot(command_prefix="!", intents=intents)
tree = bot.tree

# track all DM‐sent messages per user
active_games = defaultdict(list)

# ─── 6) UI Components ───────────────────────────────────────────
class BetModal(Modal, title="베팅 금액 입력"):
    def __init__(self, user: discord.User):
        super().__init__()
        self.user = user
        self.bet  = TextInput(label="베팅할 칩 수", placeholder="100", required=True)
        self.add_item(self.bet)
    async def on_submit(self, interaction: discord.Interaction):
        amt = int(self.bet.value)
        chips,_ = get_user_data(self.user.id)
        if not (1<=amt<=chips):
            return await interaction.response.send_message("⚠️ 잘못된 금액입니다.", ephemeral=True)
        update_user_data(self.user.id, last_bet=amt)
        await interaction.response.send_message(f"💰 `{amt}`칩으로 설정되었습니다.")
        msg = await interaction.original_response()
        active_games[self.user.id].append(msg)

class BoardSizeSelect(Select):
    def __init__(self, uid):
        opts=[discord.SelectOption(label=f"{i}×{i}", value=str(i)) for i in range(2,6)]
        super().__init__(placeholder="보드 크기 선택", min_values=1, max_values=1, options=opts)
        self.uid = uid
    async def callback(self, interaction: discord.Interaction):
        size = int(self.values[0])
        update_user_settings(self.uid, size=size)
        maxm = size*size - 1
        view = View(timeout=60)
        view.add_item(MineCountSelect(self.uid, maxm))
        await interaction.response.send_message(
            f"📐 `{size}×{size}`판 설정됨. 지뢰 (1–{maxm}) 선택하세요.", view=view
        )
        msg = await interaction.original_response()
        active_games[self.uid].append(msg)

class MineCountSelect(Select):
    def __init__(self, uid, maxm):
        opts=[discord.SelectOption(label=f"{i}개", value=str(i)) for i in range(1, maxm+1)]
        super().__init__(placeholder="지뢰 개수 선택", min_values=1, max_values=1, options=opts)
        self.uid = uid
    async def callback(self, interaction: discord.Interaction):
        m = int(self.values[0])
        update_user_settings(self.uid, mines=m)
        await interaction.response.send_message(f"💣 `{m}`개로 설정되었습니다.")
        msg = await interaction.original_response()
        active_games[self.uid].append(msg)

class SettingsView(View):
    def __init__(self, uid):
        super().__init__(timeout=60)
        self.add_item(BoardSizeSelect(uid))

class RetryView(View):
    def __init__(self, uid):
        super().__init__(timeout=60)
        self.uid = uid
    @discord.ui.button(label="🔁 다시 하기", style=discord.ButtonStyle.primary)
    async def retry(self, interaction: discord.Interaction, button: Button):
        if interaction.user.id != self.uid:
            return await interaction.response.send_message("❗ 당신의 게임이 아닙니다.", ephemeral=True)
        old = active_games.pop(self.uid, [])
        for m in old:
            try: await m.delete()
            except: pass
        await interaction.response.send_message("⌛ 잠시만 기다려주세요...")
        wait = await interaction.original_response()
        active_games[self.uid].append(wait)
        embed,view = build_menu(self.uid)
        menu = await interaction.followup.send(embed=embed, view=view)
        active_games[self.uid].append(menu)

class CashoutView(View):
    def __init__(self, uid, game):
        super().__init__(timeout=None)
        self.uid, self.game = uid, game
    @discord.ui.button(label="💸 Cashout", style=discord.ButtonStyle.primary)
    async def cash(self, interaction: discord.Interaction, button: Button):
        if interaction.user.id != self.uid or self.game["over"]:
            return
        self.game["over"]=True
        d,m,k = self.game["size"]**2, self.game["mine_count"], self.game["safe_clicked"]
        mult = calculate_stake_multiplier(d,m,k)
        rew  = int(self.game["bet"] * mult)
        chips,_ = get_user_data(self.uid)
        update_user_data(self.uid, chips=chips+rew)
        add_win(self.uid)
        cash_msg = active_games[self.uid][-1]
        e = discord.Embed(description=f"✅ Cashout! `{rew}`칩 획득 (x{mult})", color=0x00ff00)
        await cash_msg.edit(embed=e, view=RetryView(self.uid))

class MinesButton(Button):
    def __init__(self, x, y, game):
        super().__init__(label="⬜️", style=discord.ButtonStyle.secondary, row=y)
        self.x,self.y,self.game = x,y,game
        self.clicked=False
    async def callback(self, interaction: discord.Interaction):
        if self.clicked or self.game["over"] or interaction.user.id!=self.game["user_id"]:
            return await interaction.response.defer(ephemeral=True)
        self.clicked=True
        D,M,bet = self.game["size"]**2, self.game["mine_count"], self.game["bet"]
        bomb = (self.x,self.y) in self.game["mines"]
        if bomb:
            self.style,self.label=discord.ButtonStyle.danger,"💣"
            self.game["over"]=True; add_loss(self.game["user_id"])
        else:
            self.style,self.label=discord.ButtonStyle.success,"💎"
            self.game["safe_clicked"]+=1
        k= self.game["safe_clicked"]
        mult=calculate_stake_multiplier(D,M,k)
        profit=round(bet*mult,2)
        remain=(D-M)-k
        e=discord.Embed(
            description=(
                f"🎮 **{self.game['size']}×{self.game['size']}**\n"
                f"💣지뢰: {M}개   💎남은 보석: {remain}개\n"
                f"🪙베팅: {bet} Chips   🟢수익: {profit:.2f} Chips"
            ),
            color=0xff0000 if bomb else 0x00ff00
        )
        await interaction.response.edit_message(embed=e,view=self.view)
        cash_msg=active_games[self.game["user_id"]][-1]
        if bomb:
            f=discord.Embed(description="💥 실패했습니다. 다시 하시겠습니까?", color=0xff0000)
            await cash_msg.edit(embed=f,view=RetryView(self.game["user_id"]))
        elif not bomb and remain==0:
            self.game["over"]=True; add_win(self.game["user_id"])
            a=discord.Embed(description="✅ 전부 발견! 자동 Cashout", color=0x00ff00)
            await cash_msg.edit(embed=a,view=RetryView(self.game["user_id"]))

class MinesView(View):
    def __init__(self, uid, bet, mines, size):
        super().__init__(timeout=None)
        self.game={
            "user_id":uid,"bet":bet,"mine_count":mines,
            "size":size,"safe_clicked":0,
            "mines":set(random.sample([(x,y) for x in range(size) for y in range(size)],mines)),
            "over":False
        }
        for y in range(size):
            for x in range(size):
                self.add_item(MinesButton(x,y,self.game))

# ─── 7) Menu builder ───────────────────────────────────────────
def build_menu(uid:int):
    cfg    = get_user_settings(uid)
    chips,last = get_user_data(uid)
    wins,losses = get_user_stats(uid)

    embed=discord.Embed(title="MINES",color=0x00ff00)
    embed.add_field(name="💵마지막 베팅",value=f"{last}칩",inline=True)
    embed.add_field(name="💰잔액",      value=f"{chips}칩",inline=True)
    embed.add_field(name="🏆승리 수",   value=str(wins),inline=True)
    embed.add_field(name="💀패배 수",   value=str(losses),inline=True)
    embed.add_field(name="💣지뢰 수",   value=f"{cfg['mines']}개",inline=True)
    embed.add_field(name="🟩보드 크기", value=f"{cfg['size']}×{cfg['size']}",inline=True)

    view=View(timeout=60)
    view.add_item(Button(label="🔧설정",custom_id="settings",style=discord.ButtonStyle.secondary))
    view.add_item(Button(label="💵베팅 입력",custom_id="bet",style=discord.ButtonStyle.secondary))
    view.add_item(Button(label="▶️시작",custom_id="start",style=discord.ButtonStyle.success))

    async def chk(i:discord.Interaction):
        if i.user.id!=uid:
            await i.response.send_message("❗ 당신만 사용할 수 있습니다.",ephemeral=True)
            return False
        cid=i.data["custom_id"]
        if cid=="settings":
            msg=await i.response.send_message("📐보드 크기 선택:",view=SettingsView(uid),ephemeral=True)
            active_games[uid].append(await i.original_response())
        elif cid=="bet":
            await i.response.send_modal(BetModal(i.user))
        elif cid=="start":
            cfg2=get_user_settings(uid)
            size,mines=cfg2["size"],cfg2["mines"]
            chips2,last2=get_user_data(uid)
            if last2>chips2:
                return await i.response.send_message("❌ 잔액 부족",ephemeral=True)
            update_user_data(uid,chips=chips2-last2)
            mv=MinesView(uid,last2,mines,size)
            await i.response.defer(ephemeral=True)
            dm=await i.user.create_dm()
            init=discord.Embed(
                description=(
                    f"🎮 **{size}×{size}**\n"
                    f"💣지뢰: {mines}개   💎남은 보석: {size*size-mines}개\n"
                    f"🪙베팅: {last2} Chips   🟢수익: {last2*1.00:.2f} Chips"
                ),color=0x00ff00
            )
            bmsg=await dm.send(embed=init,view=mv)
            cmsg=await dm.send(embed=discord.Embed(description="💸Cashout?",color=0xffff00),
                               view=CashoutView(uid,mv.game))
            active_games[uid].extend([bmsg,cmsg])
        return True

    view.interaction_check=chk
    return embed,view

# ─── 8) Commands & Rank/Admin ───────────────────────────────────
@bot.event
async def on_ready():
    print(f"✅ Logged in as {bot.user}")
    await tree.sync(guild=test_guild)

@tree.command(name="mines",description="Mines 시작",guild=test_guild)
async def mines_cmd(inter:discord.Interaction):
    await inter.response.send_message("✅ DM으로 메뉴를 보냈습니다!",ephemeral=True)
    dm=await inter.user.create_dm()
    embed,view=build_menu(inter.user.id)
    menu=await dm.send(embed=embed,view=view)
    active_games[inter.user.id].append(menu)

@tree.command(name="clear",description="내 DM 메시지 삭제",guild=test_guild)
async def clear_cmd(inter:discord.Interaction):
    uid=inter.user.id
    msgs=active_games.pop(uid,[])
    cnt=0
    for m in msgs:
        try: await m.delete(); cnt+=1
        except: pass
    await inter.response.send_message(f"✅ {cnt}개의 DM 메시지를 삭제했습니다.",ephemeral=True)

@tree.command(
    name="chip",
    description="내 칩 잔액과 랭킹/승률을 확인합니다.",
    guild=test_guild
)
async def chip_cmd(inter: discord.Interaction):
    uid = inter.user.id
    chips, _    = get_user_data(uid)
    wins, losses= get_user_stats(uid)
    total_users= cursor.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    ranking     = [r[0] for r in cursor.execute(
                      "SELECT user_id FROM users ORDER BY chips DESC"
                   ).fetchall()]
    pos = ranking.index(str(uid)) + 1 if str(uid) in ranking else total_users
    pct = (1 - (pos-1)/total_users) * 100
    total_games = wins + losses
    wr = (wins / total_games * 100) if total_games > 0 else 0.0

    # 임베드 꾸미기
    embed = discord.Embed(
        title="💰 내 칩 & 랭킹 정보",
        color=0x1abc9c
    )
    # 아바타를 우측 상단 썸네일로
    if inter.user.avatar:
        embed.set_thumbnail(url=inter.user.avatar.url)

    # 주요 정보 필드로 추가
    embed.add_field(
        name="💰 잔액",
        value=f"**{chips:,}** 칩",
        inline=True
    )
    embed.add_field(
        name="🏅 등수",
        value=f"**{pos}** / {total_users}\n(상위 **{pct:.1f}%**)",
        inline=True
    )
    embed.add_field(
        name="📊 승률",
        value=f"**{wr:.1f}%** ({wins}승 {losses}패)",
        inline=True
    )

    # 푸터에 봇 정보
    embed.set_footer(text="Mines Bot • 즐거운 게임 되세요!")

    await inter.response.send_message(embed=embed, ephemeral=True)

@tree.command(
    name="rank",
    description="Top10 랭킹 보기",
    guild=test_guild
)
async def rank_cmd(inter: discord.Interaction):
    # DB에서 Top10 가져오기
    rows = cursor.execute(
        "SELECT user_id, chips, wins, losses FROM users ORDER BY chips DESC LIMIT 10"
    ).fetchall()

    embed = discord.Embed(title="🏆 Top 10 Chip Ranking", color=0xFFD700)
    guild = bot.get_guild(GUILD_ID)

    for idx, (uid, chips, wins, losses) in enumerate(rows, start=1):
        # 길드 캐시에 없으면 fetch
        member = guild.get_member(int(uid)) or await guild.fetch_member(int(uid))
        # display_name 사용 (길드별 닉네임 or 유저네임)
        name = member.display_name

        total_games = wins + losses
        win_rate    = (wins / total_games * 100) if total_games else 0.0

        embed.add_field(
            name=f"{idx}. {name}",
            value=f"💰 {chips}칩 | 승률 {win_rate:.1f}%",
            inline=False
        )

    await inter.response.send_message(embed=embed, ephemeral=True)


@tree.command(
    name="info",
    description="유저 정보 조회 (관리자 전용)",
    guild=test_guild
)
@app_commands.describe(
    user="정보를 조회할 대상 유저를 선택하세요"
)
@app_commands.checks.has_permissions(administrator=True)
async def info_cmd(inter: discord.Interaction, user: discord.User):
    # 1) 유저 행이 없으면 생성
    get_user_data(user.id)

    # 2) DB에서 정보 조회
    row = cursor.execute(
        """
        SELECT chips, last_bet, wins, losses, default_size, default_mines
        FROM users
        WHERE user_id = ?
        """,
        (str(user.id),)
    ).fetchone()

    # 3) 정보 없으면 알려주기
    if not row:
        return await inter.response.send_message(
            f"❌ `{user}` 의 정보가 없습니다.",
            ephemeral=True
        )

    # 4) row 언패킹
    chips, last_bet, wins, losses, df_size, df_mines = row

    # 5) 임베드 생성
    embed = discord.Embed(
        title=f"{user.display_name} ({user.id}) 정보",
        color=0x00BFFF
    )
    embed.add_field(name="💰 chips",        value=str(chips),      inline=True)
    embed.add_field(name="💵 last_bet",      value=str(last_bet),   inline=True)
    embed.add_field(name="🏆 wins",          value=str(wins),       inline=True)
    embed.add_field(name="💀 losses",        value=str(losses),     inline=True)
    embed.add_field(name="🟩 default_size",  value=str(df_size),    inline=True)
    embed.add_field(name="💣 default_mines", value=str(df_mines),   inline=True)

    # 6) 반드시 한 번은 응답!
    await inter.response.send_message(embed=embed, ephemeral=True)


@info_cmd.error
async def info_cmd_error(inter: discord.Interaction, error):
    # 권한 에러 처리
    if isinstance(error, app_commands.MissingPermissions):
        await inter.response.send_message(
            "❌ 관리자 권한이 필요합니다.",
            ephemeral=True
        )
    else:
        # 그 외 예외도 무시하지 말고 유저에게 알려주고, 로그 확인!
        await inter.response.send_message(
            "❌ 정보 조회 중 오류가 발생했습니다.",
            ephemeral=True
        )
        raise

@tree.command(name="edit",description="유저 정보 수정 (관리자)",guild=test_guild)
@app_commands.checks.has_permissions(administrator=True)
async def edit_cmd(
    inter:discord.Interaction,
    user:discord.User,
    field:str,
    value:int
):
    allowed = {
        "chips":"chips",
        "last_bet":"last_bet",
        "wins":"wins",
        "losses":"losses",
        "default_size":"default_size",
        "default_mines":"default_mines"
    }
    if field not in allowed:
        return await inter.response.send_message(
            f"❌ 수정 불가 필드: `{field}`", ephemeral=True
        )
    col = allowed[field]
    if col in ("chips","last_bet"):
        update_user_data(user.id, **{col:value})
    elif col in ("default_size","default_mines"):
        update_user_settings(user.id, **{col.split("_")[1]:value})
    else:
        cursor.execute(f"UPDATE users SET {col}=? WHERE user_id=?", (value, str(user.id)))
        conn.commit()
    await inter.response.send_message(
        f"✅ `{user}`의 `{field}`을 `{value}`로 수정했습니다.", ephemeral=True
    )

@info_cmd.error
@edit_cmd.error
async def admin_error(inter:discord.Interaction, error):
    if isinstance(error, app_commands.MissingPermissions):
        await inter.response.send_message("❌ 관리자 권한이 필요합니다.", ephemeral=True)

bot.run(DISCORD_TOKEN)
