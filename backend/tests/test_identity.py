from tests.conftest import PASSWORD


def test_login_rejects_bad_credentials_with_one_message(client, make_biz):
    make_biz()
    for body in (
        {"tenant_code": "ali-chemist", "username": "admin", "password": "wrong-pass"},
        {"tenant_code": "nope", "username": "admin", "password": PASSWORD},
        {"tenant_code": "ali-chemist", "username": "ghost", "password": PASSWORD},
    ):
        r = client.post("/api/v1/auth/login", json=body)
        assert r.status_code == 401
        assert r.json()["error"]["message"] == "invalid business code, username or password"


def test_admin_holds_all_permissions_and_tenant_has_owner(make_biz):
    biz = make_biz()
    me = biz.get("/api/v1/auth/me").json()
    assert me["role"] == "administrator"
    assert "user.manage" in me["permissions"] and "invoice.create" in me["permissions"]


def test_staff_has_nothing_until_granted_and_revocation_applies_next_request(make_biz):
    biz = make_biz()
    r = biz.post("/api/v1/users", json={"username": "cashier", "full_name": "Counter 1", "password": PASSWORD})
    assert r.status_code == 201
    uid = r.json()["id"]
    staff = {"Authorization": f"Bearer {biz.login('cashier')}"}
    assert biz.get("/api/v1/products", headers=staff).status_code == 403

    assert biz.put(f"/api/v1/users/{uid}/permissions", json={"permissions": ["product.view"]}).status_code == 200
    # The old token predates the change and is refused; a fresh login carries the new permission.
    assert biz.get("/api/v1/products", headers=staff).status_code == 401
    staff = {"Authorization": f"Bearer {biz.login('cashier')}"}
    assert biz.get("/api/v1/products", headers=staff).status_code == 200

    biz.put(f"/api/v1/users/{uid}/permissions", json={"permissions": []})
    assert biz.get("/api/v1/products", headers=staff).status_code == 401
    staff = {"Authorization": f"Bearer {biz.login('cashier')}"}
    assert biz.get("/api/v1/products", headers=staff).status_code == 403


def test_admin_only_permissions_cannot_be_granted_to_staff(make_biz):
    biz = make_biz()
    uid = biz.post("/api/v1/users", json={"username": "s1", "full_name": "S", "password": PASSWORD}).json()["id"]
    r = biz.put(f"/api/v1/users/{uid}/permissions", json={"permissions": ["user.manage"]})
    assert r.status_code == 422


def test_owner_and_last_admin_are_protected(make_biz):
    biz = make_biz()
    users = biz.get("/api/v1/users").json()
    owner = users[0]["id"]
    r = biz.put(f"/api/v1/users/{owner}/active", json={"active": False})
    assert r.status_code == 409
    second = biz.post(
        "/api/v1/users", json={"username": "admin2", "full_name": "A2", "password": PASSWORD, "role": "administrator"}
    ).json()["id"]
    assert biz.put(f"/api/v1/users/{second}/active", json={"active": False}).status_code == 200
