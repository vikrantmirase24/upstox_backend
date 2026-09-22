import datetime
import pytz
from sqlalchemy import (
    Column,
    Integer,
    String,
    Float,
    Boolean,
    ForeignKey,
    create_engine,
)
from sqlalchemy.orm import declarative_base, sessionmaker, relationship

DATABASE_URL = "postgresql://postgres:root@localhost:5432/algo_trading_db"

engine = create_engine(DATABASE_URL)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()

IST = pytz.timezone("Asia/Kolkata")


def get_current_date():
    return datetime.datetime.now(IST).strftime("%Y-%m-%d")


class User(Base):
    __tablename__ = "users"
    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, nullable=False)
    email = Column(String, unique=True, index=True, nullable=False)
    password = Column(String, nullable=False)
    role = Column(String, default="CLIENT")
    risk_multiplier = Column(Float, default=1.0)

    # Dynamic Demat & Compounding Ledger Fields
    initial_capital = Column(Float, default=10000.0)
    current_capital = Column(Float, default=10000.0)
    leverage = Column(Float, default=5.0)  # 5x MIS margin
    is_active = Column(Boolean, default=True)

    broker_account = relationship(
        "BrokerCredential",
        back_populates="owner",
        uselist=False,
        cascade="all, delete-orphan",
    )
    trades = relationship(
        "UserTrade", back_populates="trader", cascade="all, delete-orphan"
    )


class UserTrade(Base):
    __tablename__ = "user_trades"
    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"))
    trade_date = Column(String, default=get_current_date)
    symbol = Column(String, nullable=False)
    buy_price = Column(Float, nullable=False)
    buy_time = Column(String, nullable=True)  # e.g., "09:20:15 AM"
    sell_price = Column(Float, nullable=True)
    exit_time = Column(String, nullable=True)  # e.g., "03:15:30 PM"
    quantity = Column(Integer, nullable=False)
    invested_margin = Column(Float, nullable=False)
    pnl = Column(Float, default=0.0)
    status = Column(String, default="OPEN")  # "OPEN", "CLOSED"

    trader = relationship("User", back_populates="trades")


class BrokerCredential(Base):
    __tablename__ = "broker_credentials"
    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"))
    broker_name = Column(String, default="ANGEL_ONE")
    client_id = Column(String, nullable=False)
    api_key = Column(String, nullable=False)
    access_token = Column(String, nullable=True)

    owner = relationship("User", back_populates="broker_account")


class DailyWatchlist(Base):
    __tablename__ = "daily_watchlist"
    id = Column(Integer, primary_key=True, index=True)
    date = Column(String, default=get_current_date)
    symbol = Column(String, nullable=False)
    trigger_price = Column(Float, default=0.0)
    base_qty = Column(Integer, default=1)
    action = Column(String, default="BUY")
    status = Column(String, default="ACTIVE")  # ACTIVE, TRIGGERED, EXPIRED


class StockMaster(Base):
    __tablename__ = "stock_master"
    id = Column(Integer, primary_key=True, index=True)
    token = Column(String, index=True, nullable=False)
    symbol = Column(String, index=True, nullable=False)
    name = Column(String, nullable=True)
    exchange = Column(String, index=True, nullable=False)
    instrument_type = Column(String, default="EQUITY")
    lot_size = Column(Integer, default=1)
    tick_size = Column(Float, default=0.05)


Base.metadata.create_all(bind=engine)