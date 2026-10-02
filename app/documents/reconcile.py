"""Operator reconciliation of uncertain sends. Never sends anything to iiko."""

import argparse
import json

from app.documents.config import DocumentSettings
from app.documents.database import DocumentDatabase
from app.documents.policy import fail, table
from app.documents.reads import summary
from app.documents.workflow import enqueue, event, finish


def reconcile(
    database,
    kind,
    document_id,
    *,
    version=None,
    result=None,
    operator="",
    evidence="",
    writers_stopped=False,
    apply=False,
):
    parent, _ = table(kind)
    with database.connection() as db:
        db.execute(
            "SELECT operation_id FROM native_dispatch WHERE kind=%s AND document_id=%s FOR UPDATE",
            (kind, document_id),
        ).fetchall()
        doc = db.execute(
            f"SELECT * FROM {parent} WHERE id=%s FOR UPDATE", (document_id,)
        ).fetchone()
        if not doc:
            fail(404, "Документ не найден.")
        operations = db.execute(
            "SELECT * FROM portal_documents_operation WHERE kind=%s AND document_id=%s "
            "AND action='confirm' AND state IN ('pending','unknown') FOR "
            "UPDATE",
            (kind, document_id),
        ).fetchall()
        if not apply:
            return {**summary(kind, doc), "operations": [str(op["id"]) for op in operations]}
        if (
            not writers_stopped
            or not 1 <= len(operator.strip()) <= 150
            or not 10 <= len(evidence.strip()) <= 2000
            or result not in {"sent", "absent"}
        ):
            fail(422, "Остановите отправителей, укажите оператора и доказательства проверки iiko.")
        if (
            doc["version"] != version
            or doc["status"] != "Created"
            or doc["submission_state"] not in {"sending", "unknown"}
            or len(operations) != 1
            or operations[0]["result"].get("version") != version
        ):
            fail(409, "Ожидалась одна неподтверждённая отправка этой версии документа.")
        if result == "sent":
            doc = db.execute(
                f"UPDATE {parent} SET version=version+1,status='Sent',submission_state='sent',"
                "processed_at=now() WHERE id=%s RETURNING *",
                (document_id,),
            ).fetchone()
        else:
            doc = db.execute(
                f"UPDATE {parent} SET version=version+1,submission_state='idle',"
                "processed_by_id=NULL,processed_at=NULL WHERE id=%s RETURNING *",
                (document_id,),
            ).fetchone()
        op = operations[0]
        outcome = summary(kind, doc)
        finish(db, op["id"], outcome)
        db.execute(
            "UPDATE native_dispatch SET state=%s,updated_at=now() WHERE operation_id=%s",
            ("sent" if result == "sent" else "obsolete", op["id"]),
        )
        event(
            db,
            kind,
            doc,
            op["actor_id"],
            "reconciled_" + result,
            {
                "operator": operator.strip(),
                "evidence": evidence.strip(),
                "operation_id": str(op["id"]),
            },
        )
        if result == "absent":
            enqueue(db, kind, doc)
        return outcome


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("kind", choices=["waybill", "writeoff"])
    parser.add_argument("document_id", type=int)
    parser.add_argument("--version", type=int)
    parser.add_argument("--result", choices=["sent", "absent"])
    parser.add_argument("--operator", default="")
    parser.add_argument("--evidence", default="")
    parser.add_argument("--writers-stopped", action="store_true")
    parser.add_argument("--apply", action="store_true")
    options = vars(parser.parse_args())
    database = DocumentDatabase(DocumentSettings().database_url.get_secret_value())
    try:
        print(json.dumps(reconcile(database, **options), ensure_ascii=False))
    finally:
        database.close()


if __name__ == "__main__":
    main()
