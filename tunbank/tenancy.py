"""Several alliances in one bot: the registry of approved alliances, their separate databases and the services proxy.

How it fits together
  * ONE database file per alliance (data/tenants/<alliance id>/tunbank.db). A query can never see another alliance's rows
    because it cannot even open the other file. TUN is the "home" alliance: the same data/tunbank.db, same environment keys.
  * data/platform.db is the registry: which alliance uses which Discord server, who administers it, its encrypted PnW keys.
    Only the platform owner (OWNER_DISCORD_IDS) can add, suspend or resume an alliance, or correct game rules / the project
    catalogue. An alliance's own admins cannot.
  * Who is "the current alliance" is a context variable (tenantctx). Commands, buttons and modals set it from the server they
    came from; every background loop runs once per alliance inside `with tenant.activate():`.
  * `ServicesProxy` is what command code holds as `svc`. It resolves the CURRENT alliance's services each time an attribute is
    read and raises when there is none. It never falls back to the home alliance.
"""
from __future__ import annotations

import base64
import dataclasses
import hashlib
import json
import logging
from pathlib import Path

from . import tenantctx
from .db import MIGRATIONS_DIR, Database, DatabaseError
from .util import now_iso

log = logging.getLogger("tunbank.tenancy")

PLATFORM_MIGRATIONS_DIR = Path(__file__).parent / "platform_migrations"
HOME = tenantctx.HOME
STATUSES = ("ACTIVE", "SUSPENDED")


class TenancyError(Exception):
    """The message is safe to show to the person who ran the command."""


class NoCurrentAlliance(RuntimeError):
    """Code asked for an alliance's services while no alliance was in context. Deliberately NOT an AttributeError, so getattr(x, y, None) cannot hide it."""


# ---------------------------------------------------------------------------------------------------- key derivation
def _fernet_key(master: str, purpose: str, alliance_id: int) -> str:
    digest = hashlib.sha256(f"{master}|{purpose}|{alliance_id}".encode()).digest()
    return base64.urlsafe_b64encode(digest).decode()


def member_key_for(master: str, alliance_id: int) -> str:
    """The key that encrypts THIS alliance's members' API keys. Home keeps the raw environment key instead."""
    return _fernet_key(master, "tunbank-alliance", alliance_id)


def keys_key_for(master: str, alliance_id: int) -> str:
    """The key that encrypts the alliance's own PnW keys inside the platform registry (a different purpose, so a different key)."""
    return _fernet_key(master, "tunbank-alliance-pnw-keys", alliance_id)


def parse_ids(text: str) -> list:
    out = []
    for part in str(text or "").replace(";", ",").replace(" ", ",").split(","):
        part = part.strip()
        if not part:
            continue
        if not part.isdigit() or not (10 <= len(part) <= 21):
            raise TenancyError(f"'{part}' is not a Discord user ID (a number of 17-20 digits).")
        out.append(int(part))
    return list(dict.fromkeys(out))


def tenant_key(alliance_id: int) -> str:
    return f"a{int(alliance_id)}"


# ---------------------------------------------------------------------------------------------------- the registry
class Platform:
    """The platform database (data/platform.db) and who owns the platform."""

    def __init__(self, db: Database, home_settings, master_key: str = ""):
        self.db = db
        self.home_settings = home_settings
        self.owner_ids = set(home_settings.owner_ids)
        self.master_key = master_key or ""

    @classmethod
    def open(cls, home_settings) -> "Platform":
        path = Path(home_settings.data_dir) / "platform.db"
        db = Database(path, extra_migrations_dir=PLATFORM_MIGRATIONS_DIR)
        if not db.integrity_ok():
            raise DatabaseError("The platform database failed its integrity check. Not starting.")
        db.migrate(backup_dir=Path(home_settings.data_dir) / "platform-backups")
        return cls(db, home_settings, home_settings.credential_key)

    def is_owner(self, user_id) -> bool:
        try:
            return int(user_id) in self.owner_ids
        except (TypeError, ValueError):
            return False

    # ---- reading
    def rows(self) -> list:
        with self.db.read() as c:
            return [dict(r) for r in c.execute("SELECT * FROM tenants ORDER BY id")]

    def by_guild(self, guild_id) -> dict | None:
        with self.db.read() as c:
            r = c.execute("SELECT * FROM tenants WHERE guild_id=?", (int(guild_id),)).fetchone()
        return dict(r) if r else None

    def by_alliance(self, alliance_id) -> dict | None:
        with self.db.read() as c:
            r = c.execute("SELECT * FROM tenants WHERE alliance_id=?", (int(alliance_id),)).fetchone()
        return dict(r) if r else None

    # ---- owner-only changes (each one is written to the platform's own audit chain)
    def add(self, *, alliance_id: int, guild_id: int, name: str, admin_ids: list, actor: str) -> dict:
        from . import ledger as L
        alliance_id, guild_id = int(alliance_id), int(guild_id)
        if alliance_id <= 0 or guild_id <= 0:
            raise TenancyError("The alliance ID and the server ID must be positive numbers.")
        if alliance_id == self.home_settings.alliance_id:
            raise TenancyError("That is the home alliance: it is already set up through the environment variables.")
        if self.home_settings.guild_id and guild_id == self.home_settings.guild_id:
            raise TenancyError("That is the home server. Every alliance needs its own Discord server.")
        name = (name or "").strip()[:80] or f"Alliance {alliance_id}"
        with self.db.tx() as c:
            if c.execute("SELECT 1 FROM tenants WHERE alliance_id=?", (alliance_id,)).fetchone():
                raise TenancyError(f"Alliance {alliance_id} is already registered.")
            if c.execute("SELECT 1 FROM tenants WHERE guild_id=?", (guild_id,)).fetchone():
                raise TenancyError("That Discord server is already registered to an alliance.")
            now = now_iso()
            c.execute("INSERT INTO tenants(alliance_id,guild_id,name,admin_ids,status,created_at,created_by,updated_at) VALUES(?,?,?,?,?,?,?,?)",
                      (alliance_id, guild_id, name, ",".join(str(i) for i in admin_ids), "ACTIVE", now, str(actor), now))
            L.audit(c, str(actor), "TENANT_ADDED", str(alliance_id), {"guild_id": guild_id, "name": name, "admins": [str(i) for i in admin_ids]})
        return self.by_alliance(alliance_id)

    def set_status(self, alliance_id: int, status: str, actor: str) -> dict:
        from . import ledger as L
        if status not in STATUSES:
            raise TenancyError("Status must be ACTIVE or SUSPENDED.")
        with self.db.tx() as c:
            row = c.execute("SELECT * FROM tenants WHERE alliance_id=?", (int(alliance_id),)).fetchone()
            if not row:
                raise TenancyError("No such alliance is registered.")
            c.execute("UPDATE tenants SET status=?, updated_at=? WHERE alliance_id=?", (status, now_iso(), int(alliance_id)))
            L.audit(c, str(actor), "TENANT_" + status, str(alliance_id), {"before": row["status"]})
        return self.by_alliance(alliance_id)

    def set_admins(self, alliance_id: int, admin_ids: list, actor: str) -> dict:
        from . import ledger as L
        with self.db.tx() as c:
            row = c.execute("SELECT * FROM tenants WHERE alliance_id=?", (int(alliance_id),)).fetchone()
            if not row:
                raise TenancyError("No such alliance is registered.")
            c.execute("UPDATE tenants SET admin_ids=?, updated_at=? WHERE alliance_id=?",
                      (",".join(str(i) for i in admin_ids), now_iso(), int(alliance_id)))
            L.audit(c, str(actor), "TENANT_ADMINS_SET", str(alliance_id), {"before": row["admin_ids"], "after": [str(i) for i in admin_ids]})
        return self.by_alliance(alliance_id)

    # ---- the alliance's own PnW keys: encrypted at rest, never shown
    def _keys_crypto(self, alliance_id: int):
        from .credentials import Crypto, CredentialError
        if not self.master_key:
            raise CredentialError("Encrypted key storage is not set up: the owner must set CREDENTIAL_ENCRYPTION_KEY first.")
        cr = Crypto(keys_key_for(self.master_key, alliance_id))
        if not cr.enabled:
            raise CredentialError("CREDENTIAL_ENCRYPTION_KEY is not usable for key storage.")
        return cr

    def save_keys(self, alliance_id: int, keys: dict, actor: str) -> None:
        from . import ledger as L
        cr = self._keys_crypto(alliance_id)
        blob = cr.encrypt(json.dumps(keys, sort_keys=True)).decode()
        with self.db.tx() as c:
            if not c.execute("SELECT 1 FROM tenants WHERE alliance_id=?", (int(alliance_id),)).fetchone():
                raise TenancyError("No such alliance is registered.")
            c.execute("UPDATE tenants SET keys_enc=?, updated_at=? WHERE alliance_id=?", (blob, now_iso(), int(alliance_id)))
            L.audit(c, str(actor), "TENANT_KEYS_SET", str(alliance_id), {"note": "keys stored encrypted; values not recorded"})

    def load_keys(self, alliance_id: int) -> dict | None:
        row = self.by_alliance(alliance_id)
        if not row or not row["keys_enc"]:
            return None
        return json.loads(self._keys_crypto(alliance_id).decrypt(row["keys_enc"].encode()))


# ---------------------------------------------------------------------------------------------------- one alliance in memory
class Tenant:
    def __init__(self, key, alliance_id, guild_id, name, settings, db, admin_ids=(), status="ACTIVE"):
        self.key, self.alliance_id, self.guild_id, self.name = key, alliance_id, guild_id, name
        self.settings, self.db = settings, db
        self.admin_ids, self.status = set(admin_ids), status
        self.svc = None                      # built once the alliance has keys

    @property
    def is_home(self) -> bool:
        return self.key == HOME

    @property
    def has_keys(self) -> bool:
        return self.svc is not None

    @property
    def ready(self) -> bool:
        return self.svc is not None and self.status == "ACTIVE"

    def activate(self):
        return tenantctx.activate(self.key)


class Refusal(Exception):
    """A command was refused before it could run; the text is shown to the person."""


class Tenants:
    """All alliances loaded in this process. `build` is the services factory: build(settings, db) -> Services."""

    def __init__(self, platform: Platform, home: Tenant, build, data_dir: Path):
        if not home.guild_id:
            raise TenancyError("Multi-alliance mode needs GUILD_ID set: it tells the bot which server is the home alliance's.")
        self.platform, self.home, self._build, self.data_dir = platform, home, build, Path(data_dir)
        self.by_key: dict = {HOME: home}
        self.by_guild: dict = {home.guild_id: home}
        self.setup_commands = ("setup",)

    # ---- loading
    def tenant_settings(self, row: dict, keys: dict | None):
        base = self.platform.home_settings
        aid = row["alliance_id"]
        k = keys or {}
        master = self.platform.master_key
        return dataclasses.replace(
            base, alliance_id=aid, data_dir=self.data_dir / "tenants" / str(aid),
            owner_ids=set(parse_ids(row["admin_ids"])),                 # this alliance's admins; the platform owner is NOT automatically one
            guild_id=row["guild_id"], offshore=None, main_bot_key="", main_bot_api_key="", offshore_nation_id=None,
            pnw_api_key=k.get("api_key", ""), pnw_bot_key=k.get("bot_key", ""),
            pnw_bot_key_api_key=k.get("bot_key_api_key") or k.get("api_key", ""),
            credential_key=member_key_for(master, aid) if master else "")

    def _open_db(self, settings) -> Database:
        db = Database(settings.db_path)
        if not db.integrity_ok():
            raise DatabaseError(f"The database of alliance {settings.alliance_id} failed its integrity check; it was NOT loaded.")
        db.migrate(backup_dir=settings.backup_dir)
        return db

    def load_row(self, row: dict) -> Tenant:
        """(Re)load one registry row: open its database, and build its services if it has keys."""
        key = tenant_key(row["alliance_id"])
        keys = None
        try:
            keys = self.platform.load_keys(row["alliance_id"])
        except Exception as exc:  # noqa: BLE001
            log.error("alliance %s: its stored keys could not be read (%s); it stays unavailable", row["alliance_id"], type(exc).__name__)
        settings = self.tenant_settings(row, keys)
        old = self.by_key.get(key)
        db = old.db if old is not None else self._open_db(settings)
        t = Tenant(key, row["alliance_id"], row["guild_id"], row["name"], settings, db, parse_ids(row["admin_ids"]), row["status"])
        if keys and keys.get("api_key"):
            from .credentials import register_secret
            for v in keys.values():
                register_secret(v)
            if old is not None and old.svc is not None and old.settings == settings:
                t.svc = old.svc                               # nothing about its keys or admins changed: keep the running services
            else:
                self._rebuild(t)
        if old is not None:
            self.by_guild.pop(old.guild_id, None)
        self.by_key[key] = t
        self.by_guild[t.guild_id] = t
        return t

    def _rebuild(self, t: Tenant):
        """Build fresh services for the alliance (new keys mean a new PnW client)."""
        from . import icons as _icons
        from . import identity as _identity
        with t.activate():
            svc = self._build(t.settings, t.db)
            svc.alerts.only_guild_id = t.guild_id
            _identity.init(t.db)
            with t.db.read() as conn:
                _icons.load_from_db(conn)
        t.svc = svc

    def load_all(self) -> list:
        loaded = []
        for row in self.platform.rows():
            try:
                loaded.append(self.load_row(row))
            except Exception:  # noqa: BLE001 - one broken alliance must never stop the bot or the others
                log.exception("alliance %s could not be loaded; the others continue", row.get("alliance_id"))
        return loaded

    # ---- lookups
    def all(self) -> list:
        return list(self.by_key.values())

    def ready(self) -> list:
        return [t for t in self.by_key.values() if t.ready]

    def for_guild(self, guild_id):
        try:
            return self.by_guild.get(int(guild_id)) if guild_id else None
        except (TypeError, ValueError):
            return None

    def for_alliance(self, alliance_id):
        return self.by_key.get(tenant_key(alliance_id))

    def services_for(self, key: str):
        t = self.by_key.get(key)
        if t is None or t.svc is None:
            raise NoCurrentAlliance(f"alliance '{key}' has no services (unknown, or no keys yet)")
        return t.svc

    # ---- the gate
    def route(self, guild_id, command_name: str = "") -> Tenant:
        """Which alliance may use the bot from this server? Raises Refusal with a plain sentence otherwise."""
        if not guild_id:
            raise Refusal("This bot only works inside an alliance's Discord server, not in direct messages.")
        t = self.for_guild(guild_id)
        if t is None:
            raise Refusal("This server is not registered with TUN Bank. The platform owner has to approve an alliance first.")
        if t.status != "ACTIVE":
            raise Refusal("This alliance's bank is suspended by the platform owner.")
        top = (command_name or "").split(" ")[0]
        if top == "platform" and not t.is_home:
            raise Refusal("Platform commands only work in the home server.")
        if t.svc is None and top not in self.setup_commands:
            raise Refusal("This alliance's PnW keys are not set up yet. An alliance admin must run `/setup keys` first.")
        return t

    def resolve_interaction(self, interaction) -> Tenant:
        """Used by buttons and modals (no command name): the alliance for the server the press came from."""
        t = self.route(getattr(interaction, "guild_id", None), "")
        return t


# ---------------------------------------------------------------------------------------------------- the services proxy
class ServicesProxy:
    """Stands in for `svc` in the command modules. See the module docstring."""

    _SHARED = ("actions", "do_reconcile", "panel_view", "game_panel_view", "platform")

    def __init__(self, tenants_getter):
        object.__setattr__(self, "_tenants_getter", tenants_getter)
        object.__setattr__(self, "_shared", {"actions": {}, "platform": None})

    def _current(self):
        key = tenantctx.current_or_none()
        if key is None:
            raise NoCurrentAlliance("no alliance is in context: this code ran outside a command, button or alliance loop")
        return self._tenants_getter().services_for(key)

    def __getattr__(self, name):
        shared = object.__getattribute__(self, "_shared")
        if name in shared:
            return shared[name]
        if name.startswith("__"):
            raise AttributeError(name)
        return getattr(self._current(), name)

    def __setattr__(self, name, value):
        if name in self._SHARED:
            object.__getattribute__(self, "_shared")[name] = value
        else:
            setattr(self._current(), name, value)


# ---------------------------------------------------------------------------------------------------- one-time import
def import_home_game_data(platform: Platform, home_db: Database) -> dict:
    """Copy the home alliance's game-rule corrections and project-catalogue history into the platform database, ONCE.
    Nothing is removed from the home database. Afterwards the platform copy is the one in force for every alliance."""
    from . import ledger as L
    from .config import cfg_get, cfg_set
    done = {"rules": False, "catalogue_versions": 0}
    with platform.db.tx() as pc:
        if pc.execute("SELECT 1 FROM platform_settings WHERE key='home_game_data_imported'").fetchone():
            return done
        with home_db.read() as hc:
            rules = cfg_get(hc, "pnw_rule_overrides")
            versions = [dict(r) for r in hc.execute("SELECT * FROM project_catalogue_versions ORDER BY id")]
        if rules and rules != "{}" and cfg_get(pc, "pnw_rule_overrides") in ("", "{}"):
            cfg_set(pc, "pnw_rule_overrides", rules, "system:platform-import")
            done["rules"] = True
        if versions and not pc.execute("SELECT 1 FROM project_catalogue_versions").fetchone():
            for v in versions:
                pc.execute("INSERT INTO project_catalogue_versions(imported_at,imported_by,source_name,sha256,project_count,json_text) VALUES(?,?,?,?,?,?)",
                           (v["imported_at"], v["imported_by"], v["source_name"], v["sha256"], v["project_count"], v["json_text"]))
            done["catalogue_versions"] = len(versions)
        pc.execute("INSERT INTO platform_settings(key,value,updated_at,updated_by) VALUES('home_game_data_imported','1',?,?)", (now_iso(), "system:platform-import"))
        L.audit(pc, "system:platform-import", "HOME_GAME_DATA_IMPORTED", "home", done)
    return done
