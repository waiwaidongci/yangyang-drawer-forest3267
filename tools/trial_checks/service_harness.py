"""用标准库 sqlite3 验证服务层编排、持久化往返与中断恢复。

本机没有 SQLAlchemy/FastAPI，这里在独立目录提供最小垫片（仅实现服务层
实际用到的接口），加载真实的 src.services.trials 与 src.domain.trial，
配合一个 sqlite3 后备的假 Session，端到端验证：

创建试用 -> 锁定 -> 乱序收料 -> 顺序入账 -> 中断重建引擎(从库中状态)
-> 重试不重复计批 -> 首次达标 -> 负责人导出（仅一份、哈希可验）。

运行：python3 -I tools/trial_checks/service_harness.py
"""

import json
import os
import sys
import types

BACKEND = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "backend"))
sys.path.insert(0, BACKEND)

import sqlite3  # noqa: E402

DB_PATH = os.path.join(os.path.dirname(__file__), "harness.db")
if os.path.exists(DB_PATH):
    os.remove(DB_PATH)
_connection = sqlite3.connect(DB_PATH)


# --- 1. 包占位与配置模块 -----------------------------------------------------
src_pkg = types.ModuleType("src")
src_pkg.__path__ = [os.path.join(BACKEND, "src")]
config_pkg = types.ModuleType("src.config")
config_pkg.__path__ = [os.path.join(BACKEND, "src", "config")]
database_mod = types.ModuleType("src.config.database")
settings_mod = types.ModuleType("src.config.settings")
settings_mod.SEED_DEMO = False
settings_mod.DATABASE_URL = "sqlite://"
settings_mod.JWT_SECRET = "harness"
sys.modules["src"] = src_pkg
sys.modules["src.config"] = config_pkg
sys.modules["src.config.settings"] = settings_mod


# --- 2. 最小 SQLAlchemy 声明式垫片 ------------------------------------------
# 类型必须在任何模型模块导入前就绪（模型顶部 from sqlalchemy import JSON 会立即绑定）。
class _JsonType:
    """模拟 sqlalchemy.JSON：既可作类型传入 mapped_column(JSON)，也可实例化 JSON()。"""

    kind = "JSON"

    def __init__(self, *a, **k):
        pass


class _Column:
    def __init__(self, *args, **kwargs):
        marker = None
        for arg in args:
            if isinstance(arg, _JsonType) or (isinstance(arg, type) and issubclass(arg, _JsonType)):
                marker = _JsonType()
                break
        self.type_marker = marker
        self.kwargs = kwargs


def mapped_column(*args, **kwargs):
    return _Column(*args, **kwargs)


class _Mapped:
    def __class_getitem__(cls, item):
        return item


def _create_table(table, columns, table_args):
    cols_sql = ["id INTEGER PRIMARY KEY AUTOINCREMENT"]
    for col in columns:
        if col != "id":
            cols_sql.append(f"{col} TEXT")
    _connection.execute(f"CREATE TABLE IF NOT EXISTS {table} ({','.join(cols_sql)})")
    _connection.commit()


class _Meta(type):
    def __new__(mcs, name, bases, namespace):
        cls = super().__new__(mcs, name, bases, namespace)
        if "__tablename__" in namespace:
            column_objects = {k: v for k, v in namespace.items() if isinstance(v, _Column)}
            columns = list(column_objects)
            cls.__columns__ = columns
            cls.__json_columns__ = [
                k for k, col in column_objects.items()
                if col.type_marker is not None and col.type_marker.kind == "JSON"
            ]
            cls.__tablename__ = namespace["__tablename__"]
            # 保留类上的 _Column 对象（供类型识别），不覆盖为 None。
            _create_table(namespace["__tablename__"], columns, namespace.get("__table_args__"))
        return cls


class _Base(metaclass=_Meta):
    def __init__(self, **kwargs):
        for key, value in kwargs.items():
            setattr(self, key, value)


class _Clause:
    def __init__(self, table, column, value):
        self.criteria = (table, column, value)


class _Comparator:
    def __init__(self, table, name):
        self.table = table
        self.name = name

    def __eq__(self, other):
        return _Clause(self.table, self.name, other)


class _Select:
    def __init__(self, model):
        self._model = model
        self._criteria = None

    def where(self, clause):
        self._criteria = clause.criteria
        return self

    def order_by(self, *args):
        return self

    def limit(self, *args):
        return self


def _select(model):
    return _Select(model)


sa = types.ModuleType("sqlalchemy")
sa.Base = _Base
sa.mapped_column = mapped_column
sa.Mapped = _Mapped
sa.String = lambda *a, **k: None
sa.Text = lambda *a, **k: None
sa.JSON = _JsonType
sa.ForeignKey = lambda *a, **k: None
sa.UniqueConstraint = lambda *a, **k: None
sa.select = _select
sys.modules["sqlalchemy"] = sa

database_mod.Base = _Base
database_mod.engine = None

orm = types.ModuleType("sqlalchemy.orm")
orm.Mapped = _Mapped
orm.mapped_column = mapped_column
sys.modules["sqlalchemy.orm"] = orm


# --- 3. sqlite 后备的假 Session ---------------------------------------------
def _encode(value):
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False)
    return value


def _instance(model, row):
    obj = model.__new__(model)
    json_columns = model.__json_columns__
    for index, column in enumerate(model.__columns__):
        raw = row[index]
        if column in json_columns and raw is not None:
            raw = json.loads(raw)
        setattr(obj, column, raw)
    return obj


class _ScalarResult:
    def __init__(self, items):
        self._items = items

    def scalars(self):
        return self

    def all(self):
        return self._items


class _Session:
    """模拟 SQLAlchemy 的身份映射：已带 id 的对象在 commit 时走 UPDATE。"""

    def __init__(self):
        self._pending: list = []
        self._identity: dict[tuple, object] = {}

    def add(self, obj):
        self._pending.append(obj)

    def commit(self):
        for obj in self._pending:
            table = obj.__tablename__
            columns = [c for c in obj.__columns__ if c != "id"]
            cursor = _connection.execute(
                f"INSERT INTO {table} ({','.join(columns)}) VALUES ({','.join('?' * len(columns))})",
                [_encode(getattr(obj, c, None)) for c in columns],
            )
            obj.id = cursor.lastrowid
            self._identity[(table, obj.id)] = obj
        # 对已持久化、被服务层原地修改的对象执行 UPDATE（关键：模拟 SQLAlchemy 脏跟踪）。
        for (table, pk), obj in list(self._identity.items()):
            if pk is None:
                continue
            columns = [c for c in obj.__columns__ if c != "id"]
            assignments = ",".join(f"{c}=?" for c in columns)
            _connection.execute(
                f"UPDATE {table} SET {assignments} WHERE id=?",
                [_encode(getattr(obj, c, None)) for c in columns] + [pk],
            )
        _connection.commit()
        self._pending.clear()

    def refresh(self, obj):
        row = _connection.execute(
            f"SELECT * FROM {obj.__tablename__} WHERE id=?", (obj.id,),
        ).fetchone()
        json_columns = obj.__json_columns__
        for index, column in enumerate(obj.__columns__):
            raw = row[index]
            if column in json_columns and raw is not None:
                raw = json.loads(raw)
            setattr(obj, column, raw)

    def _track(self, model, row):
        key = (model.__tablename__, row[0])
        existing = self._identity.get(key)
        if existing is not None:
            return existing
        obj = _instance(model, row)
        self._identity[key] = obj
        return obj

    def get(self, model, pk):
        row = _connection.execute(
            f"SELECT * FROM {model.__tablename__} WHERE id=?", (pk,),
        ).fetchone()
        return self._track(model, row) if row else None

    def scalar(self, statement):
        table, column, value = statement._criteria
        model = statement._model
        row = _connection.execute(
            f"SELECT * FROM {table} WHERE {column}=? LIMIT 1", (value,),
        ).fetchone()
        return self._track(model, row) if row else None

    def execute(self, statement):
        model = statement._model
        rows = _connection.execute(
            f"SELECT * FROM {model.__tablename__} ORDER BY id DESC LIMIT 100",
        ).fetchall()
        return _ScalarResult([self._track(model, r) for r in rows])


database_mod.Session = _Session
database_mod.session_scope = lambda: None
sys.modules["src.config.database"] = database_mod


# --- 4. 加载真实服务与模型 ---------------------------------------------------
from src.models import trial as trial_model  # noqa: E402
from src.services import trials  # noqa: E402
from src.domain.trial import TrialEngine, verify_hash  # noqa: E402

# 让 Model.column == value 产生 _Clause（仅 where 用到的列）。
for model in (trial_model.Trial, trial_model.TrialExport):
    for col in model.__columns__:
        setattr(model, col, _Comparator(model.__tablename__, col))


class Actor:
    def __init__(self, role, actor_id):
        self.role = role
        self.id = actor_id


class Payload:
    def __init__(self, **kwargs):
        for key, value in kwargs.items():
            setattr(self, key, value)


def main():
    analyst = Actor("analyst", 1)
    admin = Actor("admin", 2)
    viewer = Actor("viewer", 3)
    session = _Session()

    info = trials.create_trial(session, Payload(label="新版滑轨试用-001", settings={}), analyst)
    trial_id = info["id"]
    print("1) 创建试用 id =", trial_id, "状态 =", info["status"])

    def lock(bid):
        preds = [{"id": f"{bid}-{i}", "old_flag": 0, "new_flag": 1} for i in range(4)]
        return trials.lock_batch(session, trial_id, Payload(batch_id=bid, predictions=preds), analyst)

    s1, s2, s3 = lock("B1"), lock("B2"), lock("B3")
    print("2) 锁定顺序号 =", s1["batch_seq"], s2["batch_seq"], s3["batch_seq"])

    # B2 先齐：顺序闸门下不得入账。
    trials.receive_outcomes(
        session, trial_id,
        Payload(batch_id="B2", outcomes=[{"id": f"B2-{i}", "failed": 1} for i in range(4)]),
        analyst,
    )
    blocked = trials.drain(session, trial_id, analyst)
    assert blocked["newly_posted"] == [], "后批先齐必须等待"
    print("3) B2 先齐时入账批数 = 0（顺序闸门生效），待入账 =",
          [b["batch_id"] for b in blocked["pending_batches"]])

    # 模拟中断：丢弃内存引擎，仅从库里 engine_state 重建。
    recovered = TrialEngine.from_state(session.get(trials.Trial, trial_id).engine_state)
    print("4) 中断恢复后待入账批 =", [b["batch_id"] for b in recovered.ledger.pending_batches()])

    # 逐条乱序收 B1（含一条幂等重复），全部实际失败，每件省 12。
    for i in range(4):
        trials.receive_one(session, trial_id,
                           Payload(batch_id="B1", item_id=f"B1-{i}", failed=1), analyst)
    trials.receive_one(session, trial_id,
                       Payload(batch_id="B1", item_id="B1-0", failed=1), analyst)

    drain1 = trials.drain(session, trial_id, analyst)
    posted_ids = [p["batch_id"] for p in drain1["newly_posted"]]
    assert posted_ids == ["B1", "B2"], posted_ids
    print("5) B1 齐套后顺序入账 =", posted_ids,
          " 累计节省 =", drain1["cumulative"]["saving"], "等效分钟")

    retry = trials.drain(session, trial_id, analyst)
    assert retry["newly_posted"] == [] and retry["cumulative"]["batches"] == 2
    print("6) 重试 drain 不重复计批，累计批次仍为 =", retry["cumulative"]["batches"])

    bid = 3
    while True:
        if bid == 3:
            name = "B3"
            trials.receive_outcomes(
                session, trial_id,
                Payload(batch_id=name, outcomes=[{"id": f"{name}-{i}", "failed": 1} for i in range(4)]),
                analyst,
            )
        else:
            name = f"B{bid}"
            lock(name)
            trials.receive_outcomes(
                session, trial_id,
                Payload(batch_id=name, outcomes=[{"id": f"{name}-{i}", "failed": 1} for i in range(4)]),
                analyst,
            )
        result = trials.drain(session, trial_id, analyst)
        if result["qualified"]:
            print("7) 首次达标顺序号 =", result["switched_at_seq"],
                  " e 值 =", round(result["e_value"], 2), "（阈值 20）")
            break
        bid += 1
        assert bid < 100

    detail = trials.get_trial(session, trial_id, analyst)
    diffs = detail["result"]["posted_batches"]
    first = diffs[0]
    print("8) 达标批次 =", detail["result"]["switch"]["batch_id"],
          " 各批代价差条数 =", len(diffs),
          " 首批 old/new/saving =", (first["old_cost"], first["new_cost"], first["saving"]))
    # 逐件证据可查。
    evidence = trials.item_evidence(session, trial_id, 1, analyst)
    assert len(evidence) == 4 and all(e["saving"] == 12 for e in evidence)

    export = trials.export_switch(session, trial_id, admin, note="负责人确认换版")
    again = trials.export_switch(session, trial_id, admin, note="重复请求返回同一凭证")
    assert again["export_id"] == export["export_id"]
    assert verify_hash(export["document"])
    print("9) 换版凭证 export_id =", export["export_id"], " 哈希校验通过，重复导出幂等")

    fetched = trials.get_export(session, trial_id, viewer)
    assert fetched["content_hash"] == export["content_hash"]
    print("10) 交班读者可查凭证，hash =", fetched["content_hash"][:16], "...")
    print("\n服务层全链路（真实 sqlite 持久化 + 中断恢复 + 幂等）验证通过。")


if __name__ == "__main__":
    main()
