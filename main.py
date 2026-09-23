from datetime import datetime
from typing import List, Optional
import random
import pytz
import pandas as pd

from fastapi import Depends, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from sqlalchemy.orm import Session

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

import database as db
import broker_service
import strategy_engine
import seed_stocks


# ============================================================
# APP
# ============================================================

app = FastAPI(title="Copy Algo Trading API")


app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ============================================================
# TIMEZONE
# ============================================================

IST = pytz.timezone("Asia/Kolkata")

scheduler = BackgroundScheduler(timezone=IST)


def get_current_date():
    """
    Current date according to India timezone.
    Format: YYYY-MM-DD
    """
    return datetime.now(IST).strftime("%Y-%m-%d")


def get_current_time():
    """
    Current time according to India timezone.
    """
    return datetime.now(IST).strftime("%I:%M:%S %p")


# ============================================================
# DATABASE
# ============================================================

def get_db():
    session = db.SessionLocal()

    try:
        yield session
    finally:
        session.close()


# ============================================================
# DAILY WATCHLIST EXPIRY
# ============================================================

def expire_old_watchlist(session: Session):
    """
    Current date se purane watchlist records ko EXPIRED karta hai.

    IMPORTANT:
    Database se records DELETE nahi hote.

    Example:

    21 Sep:
        INFY      ACTIVE
        RELIANCE  TRIGGERED

    22 Sep:
        INFY      EXPIRED
        RELIANCE  EXPIRED

    Historical reports ke liye records database me rahenge.
    """

    today = get_current_date()

    old_records = (
        session.query(db.DailyWatchlist)
        .filter(
            db.DailyWatchlist.date != today,
            db.DailyWatchlist.status.in_(["ACTIVE", "TRIGGERED"]),
        )
        .all()
    )

    if not old_records:
        return 0

    expired_count = 0

    for record in old_records:
        record.status = "EXPIRED"
        expired_count += 1

    session.commit()

    print(
        f"[WATCHLIST EXPIRY] {expired_count} old watchlist "
        f"records marked EXPIRED. Current date: {today}"
    )

    return expired_count


def expire_old_watchlist_job():
    """
    Midnight scheduler job.
    Har din 12:00 AM IST par previous-date watchlist ko EXPIRED karega.
    """

    session = db.SessionLocal()

    try:
        expire_old_watchlist(session)

    except Exception as e:
        session.rollback()
        print(f"[WATCHLIST EXPIRY ERROR] {str(e)}")

    finally:
        session.close()


# ============================================================
# DEFAULT ADMIN SEEDER
# ============================================================

@app.on_event("startup")
def seed_default_admin():

    session = db.SessionLocal()

    try:
        admin = (
            session.query(db.User)
            .filter(
                db.User.email == "admin@algo.com"
            )
            .first()
        )

        if not admin:

            new_admin = db.User(
                name="Super Admin",
                email="admin@algo.com",
                password="admin123",
                role="ADMIN",
            )

            session.add(new_admin)
            session.commit()

    finally:
        session.close()


@app.on_event("startup")
def seed_stock_master_if_empty():
    """Agar stock_master empty hai to startup par stock list auto-seed ho jayegi."""
    session = db.SessionLocal()

    try:
        stock_count = session.query(db.StockMaster).count()

        if stock_count > 0:
            print(f"[STARTUP] stock_master already contains {stock_count} records. Skipping seed.")
            return

        print("[STARTUP] stock_master is empty. Seeding stock list...")
        seed_stocks.seed_stocks()

    except Exception as exc:
        print(f"[STARTUP STOCK SEED ERROR] {exc}")

    finally:
        session.close()


# ============================================================
# SCHEMAS
# ============================================================

class LoginRequest(BaseModel):
    email: str
    password: str
    role: str


class UserCreateRequest(BaseModel):
    name: str
    email: str
    password: str
    risk_multiplier: float = 1.0
    client_id: str
    api_key: str


class UserUpdateRequest(BaseModel):
    name: str
    risk_multiplier: float
    client_id: str
    is_active: bool


class WatchlistCreateRequest(BaseModel):
    symbol: str


# ============================================================
# AUTOMATIC EXECUTION ENGINE
# ============================================================

def get_live_price(symbol: str, fallback: float = 1000.0):
    """
    Realtime LTP fetch karta hai. Upstox fail hota hai to fallback use hota hai.
    """
    live_price = broker_service.get_live_ltp(symbol)
    if live_price is not None:
        return float(live_price)
    return float(fallback)


def update_user_open_position_capital(session: Session, user):
    """
    Open trades ka live PnL calculate karke user.current_capital ko update karta hai.
    Isse capital live market jaise reflect hota hai.
    """
    open_trades = (
        session.query(db.UserTrade)
        .filter(
            db.UserTrade.user_id == user.id,
            db.UserTrade.status == "OPEN",
        )
        .all()
    )

    if not open_trades:
        return

    total_unrealized_pnl = 0.0

    for trade in open_trades:
        live_price = get_live_price(trade.symbol, trade.buy_price)
        total_unrealized_pnl += (live_price - trade.buy_price) * trade.quantity

    user.current_capital = round(
        user.initial_capital + total_unrealized_pnl,
        2,
    )


def run_auto_920_entry():
    """
    Sharp 09:20 AM par today's ACTIVE watchlist stocks par
    auto BUY order execute karta hai.
    """

    s = db.SessionLocal()

    try:

        # ----------------------------------------------------
        # IMPORTANT:
        # Purani date ke stocks ko kabhi process nahi karna.
        # ----------------------------------------------------

        today = get_current_date()

        # Safety expiry
        expire_old_watchlist(s)

        stocks = (
            s.query(db.DailyWatchlist)
            .filter(
                db.DailyWatchlist.date == today,
                db.DailyWatchlist.status == "ACTIVE",
            )
            .order_by(db.DailyWatchlist.id.asc())
            .limit(5)
            .all()
        )

        if not stocks:
            print(
                f"[AUTO 9:20 AM] Aaj ({today}) koi active stock nahi mila."
            )
            return

        users = (
            s.query(db.User)
            .filter(
                db.User.role == "CLIENT",
                db.User.is_active == True,
            )
            .all()
        )

        buy_time = get_current_time()
        num_stocks = len(stocks)

        live_ltps = {
            st.symbol: get_live_price(st.symbol, 1000.0)
            for st in stocks
        }

        # ----------------------------------------------------
        # Deploy trades
        # Equal capital allocation per stock with 5x leverage.
        # Example: 10k total capital -> 2k per stock, each position notional = 10k.
        # ----------------------------------------------------

        for user in users:

            leverage = getattr(
                user,
                "leverage",
                5.0,
            ) or 5.0

            capital_per_stock = user.current_capital / float(num_stocks)

            for st in stocks:

                cmp = live_ltps.get(st.symbol, 1000.0)
                target_notional = capital_per_stock * leverage
                qty = max(1, int(target_notional // cmp))

                # ensure used_margin roughly equals capital_per_stock
                used_margin = (qty * cmp) / leverage
                if used_margin > capital_per_stock * 1.1:
                    qty = max(1, int((capital_per_stock * leverage * 0.95) // cmp))
                    used_margin = (qty * cmp) / leverage

                trade = db.UserTrade(
                    user_id=user.id,
                    symbol=st.symbol,
                    buy_price=cmp,
                    buy_time=buy_time,
                    quantity=qty,
                    invested_margin=round(used_margin, 2),
                    status="OPEN",
                )

                s.add(trade)

                if user.broker_account:
                    broker_service.execute_user_order(
                        client_id=user.broker_account.client_id,
                        broker=user.broker_account.broker_name,
                        symbol=st.symbol,
                        qty=qty,
                        action="BUY",
                    )

        for st in stocks:
            st.status = "TRIGGERED"

        for user in users:
            update_user_open_position_capital(s, user)

        s.commit()

        print(
            f"[AUTO 9:20 AM] {len(stocks)} stocks deployed "
            f"across {len(users)} users at {buy_time}"
        )

    except Exception as e:

        s.rollback()

        print(
            f"[AUTO 9:20 AM ERROR] {str(e)}"
        )

    finally:

        s.close()


# ============================================================
# SUPER TREND EXIT
# ============================================================

def run_auto_supertrend_exit_check():
    """
    Har 5-min candle close par Supertrend check karega
    aur Red bante hi next open par exit karega.
    """

    s = db.SessionLocal()

    try:

        open_trades = (
            s.query(db.UserTrade)
            .filter(
                db.UserTrade.status == "OPEN"
            )
            .all()
        )

        if not open_trades:
            return

        exit_time = get_current_time()

        unique_symbols = list(
            set(
                [
                    t.symbol
                    for t in open_trades
                ]
            )
        )

        for sym in unique_symbols:

            # ------------------------------------------------
            # Mock 5-min OHLC
            # ------------------------------------------------

            mock_candles = pd.DataFrame(
                {
                    "open": [
                        1520,
                        1530,
                        1535,
                        1528,
                        1510,
                    ],
                    "high": [
                        1535,
                        1545,
                        1540,
                        1532,
                        1515,
                    ],
                    "low": [
                        1515,
                        1525,
                        1526,
                        1505,
                        1500,
                    ],
                    "close": [
                        1530,
                        1538,
                        1529,
                        1508,
                        1502,
                    ],
                }
            )

            st_data = (
                strategy_engine.calculate_supertrend(
                    mock_candles,
                    period=1,
                    multiplier=1.0,
                )
            )

            is_red = not st_data["is_green"].iloc[-1]

            # ------------------------------------------------
            # Exit
            # ------------------------------------------------

            if is_red:

                next_open_price = (
                    mock_candles["open"].iloc[-1]
                )

                matching_trades = (
                    s.query(db.UserTrade)
                    .filter(
                        db.UserTrade.symbol == sym,
                        db.UserTrade.status == "OPEN",
                    )
                    .all()
                )

                for tr in matching_trades:

                    tr.sell_price = next_open_price

                    tr.exit_time = exit_time

                    tr.pnl = round(
                        (
                            tr.sell_price
                            - tr.buy_price
                        )
                        * tr.quantity,
                        2,
                    )

                    tr.status = "CLOSED"

                    # Broker SELL
                    if (
                        tr.trader
                        and tr.trader.broker_account
                    ):

                        broker_service.execute_user_order(
                            client_id=(
                                tr.trader
                                .broker_account
                                .client_id
                            ),
                            broker=(
                                tr.trader
                                .broker_account
                                .broker_name
                            ),
                            symbol=tr.symbol,
                            qty=tr.quantity,
                            action="SELL",
                        )

                    # Capital update
                    if tr.trader:

                        tr.trader.current_capital = round(
                            tr.trader.current_capital
                            + tr.pnl,
                            2,
                        )

                print(
                    f"[AUTO ST EXIT] "
                    f"Exited {sym} at {next_open_price}"
                )

        s.commit()

    except Exception as e:

        s.rollback()

        print(
            f"[AUTO ST EXIT ERROR] {str(e)}"
        )

    finally:

        s.close()


# ============================================================
# 03:15 PM SQUARE OFF
# ============================================================

def run_auto_315_square_off():
    """
    Har trading day 03:15 PM par saare OPEN trades
    automatically square-off karta hai.
    """

    s = db.SessionLocal()

    try:

        open_trades = (
            s.query(db.UserTrade)
            .filter(
                db.UserTrade.status == "OPEN"
            )
            .all()
        )

        if not open_trades:

            print(
                "[AUTO 3:15 PM] Koi open trade nahi hai."
            )

            return

        exit_time = get_current_time()

        closed_count = 0

        for tr in open_trades:

            live_price = get_live_price(tr.symbol, tr.buy_price)
            exit_price = live_price

            tr.sell_price = exit_price

            tr.exit_time = exit_time

            tr.pnl = round(
                (
                    tr.sell_price
                    - tr.buy_price
                )
                * tr.quantity,
                2,
            )

            tr.status = "CLOSED"

            # Broker SELL
            if (
                tr.trader
                and tr.trader.broker_account
            ):

                broker_service.execute_user_order(
                    client_id=(
                        tr.trader
                        .broker_account
                        .client_id
                    ),
                    broker=(
                        tr.trader
                        .broker_account
                        .broker_name
                    ),
                    symbol=tr.symbol,
                    qty=tr.quantity,
                    action="SELL",
                )

            # Capital update
            if tr.trader:

                tr.trader.current_capital = round(
                    tr.trader.current_capital
                    + tr.pnl,
                    2,
                )

            closed_count += 1

        s.commit()

        print(
            f"[AUTO 3:15 PM] "
            f"{closed_count} open trades automatically "
            f"squared-off at {exit_time}"
        )

    except Exception as e:

        s.rollback()

        print(
            f"[AUTO 3:15 PM ERROR] {str(e)}"
        )

    finally:

        s.close()


# ============================================================
# SCHEDULER STARTUP
# ============================================================

@app.on_event("startup")
def init_automation_scheduler():

    # --------------------------------------------------------
    # 0. Daily watchlist expiry
    # --------------------------------------------------------
    #
    # Every midnight IST.
    # Previous day's ACTIVE/TRIGGERED records
    # become EXPIRED.
    #
    # --------------------------------------------------------

    scheduler.add_job(
        expire_old_watchlist_job,
        CronTrigger(
            hour=0,
            minute=0,
            second=1,
            timezone=IST,
        ),
        id="daily_watchlist_expiry",
        replace_existing=True,
    )

    # --------------------------------------------------------
    # 1. Sharp 09:20 AM Auto BUY
    # --------------------------------------------------------

    scheduler.add_job(
        run_auto_920_entry,
        CronTrigger(
            hour=9,
            minute=20,
            second=1,
            day_of_week="mon-fri",
            timezone=IST,
        ),
        id="auto_920_entry",
        replace_existing=True,
    )

    # --------------------------------------------------------
    # 2. Every 5-min Supertrend exit checker
    # --------------------------------------------------------

    scheduler.add_job(
        run_auto_supertrend_exit_check,
        CronTrigger(
            minute="*/5",
            hour="9-15",
            second=2,
            day_of_week="mon-fri",
            timezone=IST,
        ),
        id="supertrend_5m_exit",
        replace_existing=True,
    )

    # --------------------------------------------------------
    # 3. Sharp 03:15 PM Auto Square-Off
    # --------------------------------------------------------

    scheduler.add_job(
        run_auto_315_square_off,
        CronTrigger(
            hour=15,
            minute=15,
            second=0,
            day_of_week="mon-fri",
            timezone=IST,
        ),
        id="auto_315_square_off",
        replace_existing=True,
    )

    # --------------------------------------------------------
    # Start scheduler
    # --------------------------------------------------------

    if not scheduler.running:

        scheduler.start()

    print(
        "[SCHEDULER] Automation scheduler started."
    )


# ============================================================
# SHUTDOWN
# ============================================================

@app.on_event("shutdown")
def shutdown_automation_scheduler():

    if scheduler.running:
        scheduler.shutdown()

        print(
            "[SCHEDULER] Automation scheduler stopped."
        )


# ============================================================
# AUTH
# ============================================================

@app.post("/api/auth/login")
def login(
    req: LoginRequest,
    s: Session = Depends(get_db),
):

    user = (
        s.query(db.User)
        .filter(
            db.User.email == req.email,
            db.User.password == req.password,
            db.User.role == req.role,
        )
        .first()
    )

    if not user:

        raise HTTPException(
            status_code=401,
            detail="Invalid credentials or role mismatch!",
        )

    return {
        "id": user.id,
        "name": user.name,
        "email": user.email,
        "role": user.role,
        "multiplier": user.risk_multiplier,
        "clientId": (
            user.broker_account.client_id
            if user.broker_account
            else "N/A"
        ),
    }


# ============================================================
# MANUAL / LEGACY ENDPOINTS
# ============================================================

@app.post("/api/admin/trigger-all-5-stocks")
def trigger_daily_trades(
    s: Session = Depends(get_db),
):

    # Safety expiry
    expire_old_watchlist(s)

    today = get_current_date()

    stocks = (
        s.query(db.DailyWatchlist)
        .filter(
            db.DailyWatchlist.date == today,
            db.DailyWatchlist.status == "ACTIVE",
        )
        .order_by(db.DailyWatchlist.id.asc())
        .limit(5)
        .all()
    )

    if len(stocks) < 5:

        raise HTTPException(
            status_code=400,
            detail=(
                f"Kam se kam 5 active stocks chahiye! "
                f"Aaj ({today}) sirf {len(stocks)} active stocks hain."
            ),
        )

    users = (
        s.query(db.User)
        .filter(
            db.User.role == "CLIENT",
            db.User.is_active == True,
        )
        .all()
    )

    current_time_str = get_current_time()

    mock_prices = {
        stocks[0].symbol: 1500.0,
        stocks[1].symbol: 850.0,
        stocks[2].symbol: 2400.0,
        stocks[3].symbol: 420.0,
        stocks[4].symbol: 3100.0,
    }

    for user in users:

        total_purchasing_power = (
            user.current_capital
            * user.leverage
        )

        per_stock_budget = (
            total_purchasing_power / 5.0
        )

        for stock in stocks:

            cmp = mock_prices.get(
                stock.symbol,
                1000.0,
            )

            qty = max(
                1,
                int(per_stock_budget // cmp),
            )

            used_margin = (
                qty * cmp
            ) / user.leverage

            trade = db.UserTrade(
                user_id=user.id,
                symbol=stock.symbol,
                buy_price=cmp,
                buy_time=current_time_str,
                quantity=qty,
                invested_margin=round(
                    used_margin,
                    2,
                ),
                status="OPEN",
            )

            s.add(trade)

    for stock in stocks:
        stock.status = "TRIGGERED"

    s.commit()

    return {
        "message": (
            "5 today's stocks deployed "
            "across active users"
        )
    }


# ============================================================
# SQUARE OFF AND COMPOUND
# ============================================================

@app.post("/api/admin/square-off-and-compound")
def square_off_and_compound(
    s: Session = Depends(get_db),
):

    users = (
        s.query(db.User)
        .filter(
            db.User.role == "CLIENT"
        )
        .all()
    )

    exit_time_str = get_current_time()

    outcomes = [
        0.03,
        0.025,
        0.04,
        0.015,
        -0.02,
    ]

    for user in users:

        open_trades = (
            s.query(db.UserTrade)
            .filter(
                db.UserTrade.user_id == user.id,
                db.UserTrade.status == "OPEN",
            )
            .all()
        )

        if not open_trades:
            continue

        day_pnl = 0.0

        for idx, trade in enumerate(open_trades):

            pct = outcomes[
                idx % len(outcomes)
            ]

            trade.sell_price = round(
                trade.buy_price * (1 + pct),
                2,
            )

            trade.exit_time = exit_time_str

            trade.pnl = round(
                (
                    trade.sell_price
                    - trade.buy_price
                )
                * trade.quantity,
                2,
            )

            trade.status = "CLOSED"

            day_pnl += trade.pnl

        user.current_capital = round(
            user.current_capital + day_pnl,
            2,
        )

        s.commit()

    return {
        "message": (
            "All trades squared off "
            "and capital compounded!"
        )
    }


# ============================================================
# USER LEDGER
# ============================================================

@app.get("/api/user/ledger/{user_id}")
def get_user_ledger(
    user_id: int,
    s: Session = Depends(get_db),
):

    user = (
        s.query(db.User)
        .filter(
            db.User.id == user_id
        )
        .first()
    )

    if not user:

        raise HTTPException(
            status_code=404,
            detail="User not found",
        )

    trades = (
        s.query(db.UserTrade)
        .filter(
            db.UserTrade.user_id == user_id
        )
        .order_by(
            db.UserTrade.id.desc()
        )
        .all()
    )

    trades_data = []

    total_realized_pnl = 0.0

    for t in trades:

        current_ltp = (
            t.sell_price
            if t.status == "CLOSED"
            else round(
                t.buy_price
                * (
                    1
                    + random.uniform(
                        -0.015,
                        0.025,
                    )
                ),
                2,
            )
        )

        unrealized_pnl = (
            round(
                (
                    current_ltp
                    - t.buy_price
                )
                * t.quantity,
                2,
            )
            if t.status == "OPEN"
            else t.pnl
        )

        if t.status == "CLOSED":

            total_realized_pnl += t.pnl

        trades_data.append(
            {
                "id": t.id,
                "symbol": t.symbol,
                "buy_price": t.buy_price,
                "buy_time": t.buy_time or "-",
                "sell_price": t.sell_price,
                "exit_time": t.exit_time or "-",
                "current_ltp": current_ltp,
                "quantity": t.quantity,
                "invested_margin": t.invested_margin,
                "pnl": unrealized_pnl,
                "status": t.status,
            }
        )

    return {
        "current_capital": user.current_capital,
        "purchasing_power": (
            user.current_capital
            * user.leverage
        ),
        "total_realized_pnl": round(
            total_realized_pnl,
            2,
        ),
        "trades": trades_data,
    }


# ============================================================
# USERS CRUD
# ============================================================

@app.get("/api/admin/users")
def list_users(
    s: Session = Depends(get_db),
):

    users = (
        s.query(db.User)
        .filter(
            db.User.role == "CLIENT"
        )
        .all()
    )

    return [
        {
            "id": u.id,
            "name": u.name,
            "email": u.email,
            "role": u.role,
            "multiplier": u.risk_multiplier,
            "is_active": u.is_active,
            "client_id": (
                u.broker_account.client_id
                if u.broker_account
                else "N/A"
            ),
            "api_key": (
                u.broker_account.api_key
                if u.broker_account
                else "N/A"
            ),
        }
        for u in users
    ]


@app.post("/api/admin/users")
def create_user(
    req: UserCreateRequest,
    s: Session = Depends(get_db),
):

    existing = (
        s.query(db.User)
        .filter(
            db.User.email == req.email
        )
        .first()
    )

    if existing:

        raise HTTPException(
            status_code=400,
            detail="User email already exists!",
        )

    user = db.User(
        name=req.name,
        email=req.email,
        password=req.password,
        role="CLIENT",
        risk_multiplier=req.risk_multiplier,
        is_active=True,
    )

    s.add(user)

    s.commit()

    s.refresh(user)

    cred = db.BrokerCredential(
        user_id=user.id,
        client_id=req.client_id,
        api_key=req.api_key,
    )

    s.add(cred)

    s.commit()

    return {
        "message": "User created successfully"
    }


@app.put("/api/admin/users/{user_id}")
def update_user(
    user_id: int,
    req: UserUpdateRequest,
    s: Session = Depends(get_db),
):

    user = (
        s.query(db.User)
        .filter(
            db.User.id == user_id
        )
        .first()
    )

    if not user:

        raise HTTPException(
            status_code=404,
            detail="User not found",
        )

    user.name = req.name
    user.risk_multiplier = req.risk_multiplier
    user.is_active = req.is_active

    if user.broker_account:

        user.broker_account.client_id = (
            req.client_id
        )

    s.commit()

    return {
        "message": "User updated successfully"
    }


@app.delete("/api/admin/users/{user_id}")
def delete_user(
    user_id: int,
    s: Session = Depends(get_db),
):

    user = (
        s.query(db.User)
        .filter(
            db.User.id == user_id
        )
        .first()
    )

    if not user:

        raise HTTPException(
            status_code=404,
            detail="User not found",
        )

    if user.broker_account:
        s.delete(user.broker_account)

    s.delete(user)

    s.commit()

    return {
        "message": "User deleted successfully"
    }


# ============================================================
# WATCHLIST / STOCKS
# ============================================================

@app.get("/api/watchlist")
def get_watchlist(
    s: Session = Depends(get_db),
):
    """
    Frontend ko SIRF TODAY ki watchlist milegi.

    Old records database me rahenge,
    lekin frontend par nahi dikhenge.
    """

    # Safety:
    # Agar midnight scheduler miss hua ho to bhi
    # purane records expire ho jayenge.
    expire_old_watchlist(s)

    today = get_current_date()

    stocks = (
        s.query(db.DailyWatchlist)
        .filter(
            db.DailyWatchlist.date == today
        )
        .order_by(
            db.DailyWatchlist.id.asc()
        )
        .all()
    )

    return [
        {
            "id": item.id,
            "date": item.date,
            "symbol": item.symbol,
            "trigger_price": item.trigger_price,
            "base_qty": item.base_qty,
            "action": item.action,
            "status": item.status,
        }
        for item in stocks
    ]


@app.post("/api/admin/watchlist")
def add_watchlist(
    req: WatchlistCreateRequest,
    s: Session = Depends(get_db),
):
    """
    Stock ko TODAY ki watchlist me add karta hai.

    Date frontend se nahi li jaati.
    Backend IST se today's date set karta hai.
    """

    # Safety expiry
    expire_old_watchlist(s)

    today = get_current_date()

    symbol = req.symbol.strip().upper()

    if not symbol:

        raise HTTPException(
            status_code=400,
            detail="Stock symbol is required",
        )

    # --------------------------------------------------------
    # Same stock same day duplicate check
    # --------------------------------------------------------

    existing = (
        s.query(db.DailyWatchlist)
        .filter(
            db.DailyWatchlist.date == today,
            db.DailyWatchlist.symbol == symbol,
            db.DailyWatchlist.status.in_(
                [
                    "ACTIVE",
                    "TRIGGERED",
                ]
            ),
        )
        .first()
    )

    if existing:

        raise HTTPException(
            status_code=400,
            detail=(
                f"{symbol} already exists "
                f"in today's watchlist."
            ),
        )

    # --------------------------------------------------------
    # Create today's watchlist record
    # --------------------------------------------------------

    item = db.DailyWatchlist(
        date=today,
        symbol=symbol,
        trigger_price=0.0,
        base_qty=1,
        action="BUY",
        status="ACTIVE",
    )

    s.add(item)

    s.commit()

    s.refresh(item)

    return {
        "success": True,
        "message": (
            f"{symbol} added to "
            f"{today} watchlist"
        ),
        "data": {
            "id": item.id,
            "date": item.date,
            "symbol": item.symbol,
            "trigger_price": item.trigger_price,
            "base_qty": item.base_qty,
            "action": item.action,
            "status": item.status,
        },
    }


@app.delete("/api/admin/watchlist/{item_id}")
def delete_watchlist(
    item_id: int,
    s: Session = Depends(get_db),
):
    """
    Manual delete button.

    IMPORTANT:
    Automatic date change par ye endpoint use nahi hota.
    Date rollover EXPIRED status se hota hai.
    """

    item = (
        s.query(db.DailyWatchlist)
        .filter(
            db.DailyWatchlist.id == item_id
        )
        .first()
    )

    if not item:

        raise HTTPException(
            status_code=404,
            detail="Item not found",
        )

    s.delete(item)

    s.commit()

    return {
        "message": "Watchlist item removed"
    }


# ============================================================
# HISTORICAL WATCHLIST
# ============================================================

@app.get("/api/admin/watchlist/history")
def get_watchlist_history(
    s: Session = Depends(get_db),
):
    """
    Saare dates ki watchlist.
    Reports/history ke liye.
    """

    stocks = (
        s.query(db.DailyWatchlist)
        .order_by(
            db.DailyWatchlist.date.desc(),
            db.DailyWatchlist.id.desc(),
        )
        .all()
    )

    return [
        {
            "id": item.id,
            "date": item.date,
            "symbol": item.symbol,
            "trigger_price": item.trigger_price,
            "base_qty": item.base_qty,
            "action": item.action,
            "status": item.status,
        }
        for item in stocks
    ]


# ============================================================
# EXECUTION & REPORTS
# ============================================================

@app.post("/api/admin/trigger/{item_id}")
def trigger_orders(
    item_id: int,
    s: Session = Depends(get_db),
):

    # Safety expiry
    expire_old_watchlist(s)

    today = get_current_date()

    item = (
        s.query(db.DailyWatchlist)
        .filter(
            db.DailyWatchlist.id == item_id,
            db.DailyWatchlist.date == today,
        )
        .first()
    )

    if not item:

        raise HTTPException(
            status_code=404,
            detail=(
                "Stock not found in "
                "today's watchlist"
            ),
        )

    if item.status != "ACTIVE":

        raise HTTPException(
            status_code=400,
            detail=(
                f"Stock status is {item.status}. "
                f"Only ACTIVE stocks can be triggered."
            ),
        )

    clients = (
        s.query(db.User)
        .filter(
            db.User.role == "CLIENT",
            db.User.is_active == True,
        )
        .all()
    )

    for client in clients:

        if client.broker_account:

            calculated_qty = int(
                item.base_qty
                * client.risk_multiplier
            )

            broker_service.execute_user_order(
                client_id=(
                    client.broker_account.client_id
                ),
                broker=(
                    client.broker_account.broker_name
                ),
                symbol=item.symbol,
                qty=max(
                    1,
                    calculated_qty,
                ),
                action=item.action,
            )

    item.status = "TRIGGERED"

    s.commit()

    return {
        "message": (
            f"Orders executed across "
            f"{len(clients)} active accounts"
        )
    }


@app.get("/api/admin/reports")
def get_reports(
    s: Session = Depends(get_db),
):

    # Important:
    # Reports ke liye old records ko delete/filter nahi karna.
    stocks = (
        s.query(db.DailyWatchlist)
        .order_by(
            db.DailyWatchlist.date.desc(),
            db.DailyWatchlist.id.desc(),
        )
        .all()
    )

    users_count = (
        s.query(db.User)
        .filter(
            db.User.role == "CLIENT",
            db.User.is_active == True,
        )
        .count()
    )

    report_data = []

    for st in stocks:

        report_data.append(
            {
                "id": st.id,
                "date": st.date,
                "symbol": st.symbol,
                "action": st.action,
                "trigger_price": st.trigger_price,
                "status": st.status,
                "accounts_targeted": (
                    users_count
                    if st.status == "TRIGGERED"
                    else 0
                ),
                "total_lots_executed": (
                    st.base_qty * users_count
                    if st.status == "TRIGGERED"
                    else 0
                ),
            }
        )

    return report_data


# ============================================================
# STOCK SEARCH
# ============================================================

@app.get("/api/stocks/search")
def search_stocks(
    query: str,
    exchange: str = "NSE",
    s: Session = Depends(get_db),
):

    if len(query) < 2:
        return []

    results = (
        s.query(db.StockMaster)
        .filter(
            db.StockMaster.exchange == exchange,
            db.StockMaster.symbol.ilike(
                f"%{query}%"
            ),
        )
        .limit(20)
        .all()
    )

    return [
        {
            "token": r.token,
            "symbol": r.symbol,
            "name": r.name,
            "exchange": r.exchange,
            "lot_size": r.lot_size,
        }
        for r in results
    ]


# ============================================================
# 09:20 MANUAL ENTRY
# ============================================================

@app.post("/api/admin/trigger-920-entry")
def trigger_920_entry(
    s: Session = Depends(get_db),
):

    # Safety expiry
    expire_old_watchlist(s)

    today = get_current_date()

    stocks = (
        s.query(db.DailyWatchlist)
        .filter(
            db.DailyWatchlist.date == today,
            db.DailyWatchlist.status == "ACTIVE",
        )
        .order_by(
            db.DailyWatchlist.id.asc()
        )
        .limit(5)
        .all()
    )

    if len(stocks) < 5:

        raise HTTPException(
            status_code=400,
            detail=(
                f"Kam se kam 5 active stocks "
                f"today's list me hone chahiye! "
                f"Abhi {len(stocks)} hain."
            ),
        )

    users = (
        s.query(db.User)
        .filter(
            db.User.role == "CLIENT",
            db.User.is_active == True,
        )
        .all()
    )

    buy_time = get_current_time()

    mock_ltps = {
        stocks[0].symbol: 1540.0,
        stocks[1].symbol: 825.0,
        stocks[2].symbol: 2450.0,
        stocks[3].symbol: 410.0,
        stocks[4].symbol: 3180.0,
    }

    for user in users:

        total_margin = (
            user.current_capital
            * user.leverage
        )

        slot_budget = (
            total_margin / 5.0
        )

        for st in stocks:

            cmp = mock_ltps.get(
                st.symbol,
                1000.0,
            )

            qty = max(
                1,
                int(slot_budget // cmp),
            )

            used_margin = (
                qty * cmp
            ) / user.leverage

            trade = db.UserTrade(
                user_id=user.id,
                symbol=st.symbol,
                buy_price=cmp,
                buy_time=buy_time,
                quantity=qty,
                invested_margin=round(
                    used_margin,
                    2,
                ),
                status="OPEN",
            )

            s.add(trade)

            if user.broker_account:

                broker_service.execute_user_order(
                    client_id=(
                        user.broker_account.client_id
                    ),
                    broker=(
                        user.broker_account.broker_name
                    ),
                    symbol=st.symbol,
                    qty=qty,
                    action="BUY",
                )

    for st in stocks:
        st.status = "TRIGGERED"

    s.commit()

    return {
        "message": (
            "9:20 AM Heikin-Ashi Entry "
            "deployed across active "
            "client accounts"
        ),
        "date": today,
        "stocks": [
            st.symbol
            for st in stocks
        ],
    }


# ============================================================
# SUPERTREND EXIT MANUAL API
# ============================================================

@app.post("/api/admin/check-supertrend-exit")
def check_supertrend_exit(
    s: Session = Depends(get_db),
):

    open_trades = (
        s.query(db.UserTrade)
        .filter(
            db.UserTrade.status == "OPEN"
        )
        .all()
    )

    if not open_trades:

        return {
            "message": "Koi open trade nahi hai."
        }

    exit_time = get_current_time()

    exited_symbols = []

    unique_symbols = list(
        set(
            [
                t.symbol
                for t in open_trades
            ]
        )
    )

    for sym in unique_symbols:

        mock_candles = pd.DataFrame(
            {
                "open": [
                    1520,
                    1530,
                    1535,
                    1528,
                    1510,
                ],
                "high": [
                    1535,
                    1545,
                    1540,
                    1532,
                    1515,
                ],
                "low": [
                    1515,
                    1525,
                    1526,
                    1505,
                    1500,
                ],
                "close": [
                    1530,
                    1538,
                    1529,
                    1508,
                    1502,
                ],
            }
        )

        st_data = (
            strategy_engine.calculate_supertrend(
                mock_candles,
                period=1,
                multiplier=1.0,
            )
        )

        is_red = not st_data[
            "is_green"
        ].iloc[-1]

        if is_red:

            next_open_price = (
                mock_candles["open"].iloc[-1]
            )

            matching_trades = (
                s.query(db.UserTrade)
                .filter(
                    db.UserTrade.symbol == sym,
                    db.UserTrade.status == "OPEN",
                )
                .all()
            )

            for tr in matching_trades:

                tr.sell_price = next_open_price

                tr.exit_time = exit_time

                tr.pnl = round(
                    (
                        tr.sell_price
                        - tr.buy_price
                    )
                    * tr.quantity,
                    2,
                )

                tr.status = "CLOSED"

                if tr.trader:

                    tr.trader.current_capital = round(
                        tr.trader.current_capital
                        + tr.pnl,
                        2,
                    )

            exited_symbols.append(sym)

    s.commit()

    return {
        "message": (
            "Supertrend Red check complete. "
            f"Exited stocks: {exited_symbols}"
        ),
        "exited_count": len(exited_symbols),
    }