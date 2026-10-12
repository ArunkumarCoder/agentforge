"""Reference OpenAPI specs that score full marks on the API Design golden cases.

They are built from small helpers, so each one reads like a design rather than 200 lines of YAML.
The tests use them as the model's perfect answer and to prove the cases measure what they claim.
"""

from typing import Any

from agentforge.tools.openapi import dump_spec

ERROR = {"$ref": "#/components/responses/Error"}


def ref(kind: str, name: str) -> dict[str, str]:
    return {"$ref": f"#/components/{kind}/{name}"}


def body(schema: dict[str, Any], media: str = "application/json") -> dict[str, Any]:
    return {"required": True, "content": {media: {"schema": schema}}}


def reply(description: str, schema: dict[str, Any] | None = None, media: str = "application/json"):
    out: dict[str, Any] = {"description": description}
    if schema is not None:
        out["content"] = {media: {"schema": schema}}
    return out


def path_param(name: str, kind: str = "string") -> dict[str, Any]:
    return {"name": name, "in": "path", "required": True, "schema": {"type": kind}}


def query(name: str, kind: str = "string", **extra: Any) -> dict[str, Any]:
    return {"name": name, "in": "query", "schema": {"type": kind, **extra}}


def operation(
    op_id: str,
    summary: str,
    tag: str,
    *,
    ok: tuple[str, dict[str, Any]],
    params: list[dict[str, Any]] | None = None,
    request: dict[str, Any] | None = None,
    **extra: Any,
) -> dict[str, Any]:
    code, response = ok
    node: dict[str, Any] = {
        "operationId": op_id,
        "summary": summary,
        "description": f"{summary}.",
        "tags": [tag],
    }
    if params:
        node["parameters"] = params
    if request:
        node["requestBody"] = request
    node["responses"] = {code: response, "default": ERROR}
    node.update(extra)
    return node


def page(item: str, mode: str) -> dict[str, Any]:
    props: dict[str, Any] = {"items": {"type": "array", "items": ref("schemas", item)}}
    props["total" if mode == "offset" else "nextCursor"] = (
        {"type": "integer"} if mode == "offset" else {"type": ["string", "null"]}
    )
    return {"type": "object", "required": ["items"], "properties": props}


def paging(mode: str) -> list[dict[str, Any]]:
    names = ["Limit", "Offset" if mode == "offset" else "Cursor"]
    return [ref("parameters", n) for n in names]


def base(title: str, schemas: dict[str, Any], paths: dict[str, Any], **top: Any) -> dict[str, Any]:
    components: dict[str, Any] = {
        "schemas": {
            "Error": {
                "type": "object",
                "required": ["code", "message"],
                "properties": {"code": {"type": "string"}, "message": {"type": "string"}},
            },
            **schemas,
        },
        "responses": {"Error": reply("Something went wrong", ref("schemas", "Error"))},
        "parameters": {
            "Limit": query("limit", "integer", minimum=1, maximum=100),
            "Offset": query("offset", "integer", minimum=0),
            "Cursor": query("cursor"),
        },
    }
    components.update(top.pop("components", {}))
    return {
        "openapi": "3.1.0",
        "info": {"title": title, "version": "1.0.0", "description": f"{title}."},
        **top,
        "paths": paths,
        "components": components,
    }


BEARER = {"bearerAuth": {"type": "http", "scheme": "bearer"}}


def tasks() -> dict[str, Any]:
    task = {
        "type": "object",
        "required": ["id", "title", "done"],
        "properties": {
            "id": {"type": "string", "format": "uuid"},
            "title": {"type": "string", "maxLength": 200},
            "done": {"type": "boolean"},
            "due_date": {"type": ["string", "null"], "format": "date"},
        },
    }
    new_task = {
        "type": "object",
        "required": ["title"],
        "properties": {
            "title": {"type": "string", "maxLength": 200},
            "due_date": {"type": "string", "format": "date"},
        },
    }
    patch = {
        "type": "object",
        "properties": {
            "title": {"type": "string", "maxLength": 200},
            "done": {"type": "boolean"},
            "due_date": {"type": "string", "format": "date"},
        },
    }
    tid = [path_param("id")]
    t = ref("schemas", "Task")
    paths = {
        "/tasks": {
            "get": operation(
                "listTasks",
                "List tasks",
                "tasks",
                ok=("200", reply("A page of tasks", page("Task", "offset"))),
                params=[*paging("offset"), query("done", "boolean")],
            ),
            "post": operation(
                "createTask",
                "Create a task",
                "tasks",
                ok=("201", reply("The new task", t)),
                request=body(ref("schemas", "NewTask")),
            ),
        },
        "/tasks/{id}": {
            "get": operation(
                "getTask", "Get a task", "tasks", ok=("200", reply("The task", t)), params=tid
            ),
            "patch": operation(
                "updateTask",
                "Update a task",
                "tasks",
                ok=("200", reply("The updated task", t)),
                params=tid,
                request=body(ref("schemas", "TaskPatch")),
            ),
            "delete": operation(
                "deleteTask", "Delete a task", "tasks", ok=("204", reply("Deleted")), params=tid
            ),
        },
        "/tasks/{id}/complete": {
            "post": operation(
                "completeTask",
                "Mark a task as done",
                "tasks",
                ok=("200", reply("The completed task", t)),
                params=tid,
            )
        },
    }
    spec = base(
        "Task API",
        {"Task": task, "NewTask": new_task, "TaskPatch": patch},
        paths,
        components={"securitySchemes": BEARER},
        security=[{"bearerAuth": []}],
    )
    return spec


def bookstore() -> dict[str, Any]:
    author = {
        "type": "object",
        "required": ["id", "name"],
        "properties": {
            "id": {"type": "string"},
            "name": {"type": "string"},
            "born": {"type": "integer"},
        },
    }
    book = {
        "type": "object",
        "required": ["isbn", "title", "author_id", "price"],
        "properties": {
            "isbn": {"type": "string"},
            "title": {"type": "string"},
            "author_id": {"type": "string"},
            "price": {"type": "number", "minimum": 0},
        },
    }
    new_author = {
        "type": "object",
        "required": ["name"],
        "properties": {"name": {"type": "string"}, "born": {"type": "integer"}},
    }
    a, b = ref("schemas", "Author"), ref("schemas", "Book")
    aid, isbn = [path_param("authorId")], [path_param("isbn")]
    paths = {
        "/authors": {
            "get": operation(
                "listAuthors",
                "List authors",
                "authors",
                ok=("200", reply("Authors", page("Author", "cursor"))),
                params=paging("cursor"),
            ),
            "post": operation(
                "createAuthor",
                "Create an author",
                "authors",
                ok=("201", reply("The author", a)),
                request=body(ref("schemas", "NewAuthor")),
            ),
        },
        "/authors/{authorId}": {
            "get": operation(
                "getAuthor",
                "Get an author",
                "authors",
                ok=("200", reply("The author", a)),
                params=aid,
            )
        },
        "/authors/{authorId}/books": {
            "get": operation(
                "listAuthorBooks",
                "List an author's books",
                "authors",
                ok=("200", reply("Books", page("Book", "cursor"))),
                params=[*aid, *paging("cursor")],
            )
        },
        "/books": {
            "get": operation(
                "listBooks",
                "List books",
                "books",
                ok=("200", reply("Books", page("Book", "cursor"))),
                params=paging("cursor"),
            ),
            "post": operation(
                "createBook",
                "Add a book",
                "books",
                ok=("201", reply("The book", b)),
                request=body(b),
            ),
        },
        "/books/{isbn}": {
            "get": operation(
                "getBook", "Get a book", "books", ok=("200", reply("The book", b)), params=isbn
            ),
            "put": operation(
                "replaceBook",
                "Replace a book",
                "books",
                ok=("200", reply("The book", b)),
                params=isbn,
                request=body(b),
            ),
        },
    }
    return base("Bookstore API", {"Author": author, "NewAuthor": new_author, "Book": book}, paths)


def auth_users() -> dict[str, Any]:
    user = {
        "type": "object",
        "required": ["id", "email", "role"],
        "properties": {
            "id": {"type": "string"},
            "email": {"type": "string", "format": "email"},
            "name": {"type": "string"},
            "role": {"type": "string", "enum": ["member", "admin"]},
        },
    }
    creds = {
        "type": "object",
        "required": ["email", "password"],
        "properties": {
            "email": {"type": "string", "format": "email"},
            "password": {"type": "string", "minLength": 8},
        },
    }
    register = {
        "type": "object",
        "required": ["email", "password"],
        "properties": {**creds["properties"], "name": {"type": "string"}},
    }
    token = {
        "type": "object",
        "required": ["access_token", "expires_in"],
        "properties": {"access_token": {"type": "string"}, "expires_in": {"type": "integer"}},
    }
    me_patch = {
        "type": "object",
        "properties": {"name": {"type": "string"}, "password": {"type": "string", "minLength": 8}},
    }
    u = ref("schemas", "User")
    paths = {
        "/auth/login": {
            "post": operation(
                "login",
                "Log in with email and password",
                "auth",
                ok=("200", reply("A bearer token", ref("schemas", "Token"))),
                request=body(ref("schemas", "Credentials")),
                security=[],
            )
        },
        "/users": {
            "post": operation(
                "registerUser",
                "Register a new user",
                "users",
                ok=("201", reply("The new user", u)),
                request=body(ref("schemas", "Registration")),
                security=[],
            )
        },
        "/users/me": {
            "get": operation(
                "getCurrentUser",
                "Get the signed-in user",
                "users",
                ok=("200", reply("The user", u)),
            ),
            "patch": operation(
                "updateCurrentUser",
                "Update the signed-in user",
                "users",
                ok=("200", reply("The user", u)),
                request=body(ref("schemas", "ProfilePatch")),
            ),
        },
        "/users/{id}": {
            "get": operation(
                "getUser",
                "Get any user (admins only)",
                "users",
                ok=("200", reply("The user", u)),
                params=[path_param("id")],
            )
        },
    }
    return base(
        "Accounts API",
        {
            "User": user,
            "Credentials": creds,
            "Registration": register,
            "Token": token,
            "ProfilePatch": me_patch,
        },
        paths,
        components={"securitySchemes": BEARER},
        security=[{"bearerAuth": []}],
    )


def documents() -> dict[str, Any]:
    doc = {
        "type": "object",
        "required": ["id", "title", "size_bytes"],
        "properties": {
            "id": {"type": "string"},
            "title": {"type": "string"},
            "size_bytes": {"type": "integer"},
            "uploaded_at": {"type": "string", "format": "date-time"},
        },
    }
    upload = {
        "type": "object",
        "required": ["file", "title"],
        "properties": {
            "file": {"type": "string", "contentMediaType": "application/pdf"},
            "title": {"type": "string"},
        },
    }
    d = ref("schemas", "Document")
    did = [path_param("id")]
    paths = {
        "/documents": {
            "get": operation(
                "listDocuments",
                "List documents",
                "documents",
                ok=("200", reply("Documents", {"type": "array", "items": d})),
            ),
            "post": operation(
                "uploadDocument",
                "Upload a PDF",
                "documents",
                ok=("201", reply("The stored document", d)),
                request=body(ref("schemas", "Upload"), "multipart/form-data"),
            ),
        },
        "/documents/{id}": {
            "get": operation(
                "getDocument",
                "Get a document's details",
                "documents",
                ok=("200", reply("The document", d)),
                params=did,
            ),
            "delete": operation(
                "deleteDocument",
                "Delete a document",
                "documents",
                ok=("204", reply("Deleted")),
                params=did,
            ),
        },
        "/documents/{id}/content": {
            "get": operation(
                "downloadDocument",
                "Download the PDF",
                "documents",
                ok=(
                    "200",
                    reply(
                        "The PDF file", {"type": "string", "format": "binary"}, "application/pdf"
                    ),
                ),
                params=did,
            )
        },
    }
    return base(
        "Document Store API",
        {"Document": doc, "Upload": upload},
        paths,
        components={
            "securitySchemes": {
                "apiKeyAuth": {"type": "apiKey", "in": "header", "name": "X-API-Key"}
            }
        },
        security=[{"apiKeyAuth": []}],
    )


def orders() -> dict[str, Any]:
    status = {"type": "string", "enum": ["pending", "paid", "shipped", "cancelled"]}
    item = {
        "type": "object",
        "required": ["sku", "quantity"],
        "properties": {
            "sku": {"type": "string"},
            "quantity": {"type": "integer", "minimum": 1},
            "unit_price": {"type": "number"},
        },
    }
    order = {
        "type": "object",
        "required": ["id", "status", "items", "total"],
        "properties": {
            "id": {"type": "string"},
            "status": ref("schemas", "OrderStatus"),
            "items": {"type": "array", "items": ref("schemas", "OrderItem")},
            "total": {"type": "number"},
            "created_at": {"type": "string", "format": "date-time"},
        },
    }
    new_order = {
        "type": "object",
        "required": ["items"],
        "properties": {
            "items": {"type": "array", "minItems": 1, "items": ref("schemas", "OrderItem")}
        },
    }
    o = ref("schemas", "Order")
    oid = [path_param("orderId")]
    paths = {
        "/orders": {
            "get": operation(
                "listOrders",
                "List orders",
                "orders",
                ok=("200", reply("Orders", page("Order", "cursor"))),
                params=[
                    *paging("cursor"),
                    {"name": "status", "in": "query", "schema": ref("schemas", "OrderStatus")},
                ],
            ),
            "post": operation(
                "createOrder",
                "Place an order",
                "orders",
                ok=("201", reply("The order", o)),
                request=body(ref("schemas", "NewOrder")),
            ),
        },
        "/orders/{orderId}": {
            "get": operation(
                "getOrder", "Get an order", "orders", ok=("200", reply("The order", o)), params=oid
            )
        },
        "/orders/{orderId}/items": {
            "get": operation(
                "listOrderItems",
                "List an order's items",
                "orders",
                ok=("200", reply("Items", page("OrderItem", "cursor"))),
                params=[*oid, *paging("cursor")],
            )
        },
        "/orders/{orderId}/cancel": {
            "post": operation(
                "cancelOrder",
                "Cancel an order",
                "orders",
                ok=("200", reply("The cancelled order", o)),
                params=oid,
            )
        },
        "/orders/{orderId}/ship": {
            "post": operation(
                "shipOrder",
                "Mark an order as shipped",
                "orders",
                ok=("200", reply("The shipped order", o)),
                params=oid,
            )
        },
    }
    return base(
        "Orders API",
        {"OrderStatus": status, "OrderItem": item, "Order": order, "NewOrder": new_order},
        paths,
        components={"securitySchemes": BEARER},
        security=[{"bearerAuth": []}],
    )


REFERENCE_SPECS = {
    "tasks": dump_spec(tasks()),
    "bookstore": dump_spec(bookstore()),
    "auth_users": dump_spec(auth_users()),
    "documents": dump_spec(documents()),
    "orders": dump_spec(orders()),
}
