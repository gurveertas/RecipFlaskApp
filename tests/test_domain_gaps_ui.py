from webapp.app import create_app


def test_domain_gaps_uses_the_shared_app_shell():
    response = create_app().test_client().get("/admin/domains")

    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert '<main class="app-main">' in html
    assert 'class="usage-chip"' in html
    assert 'id="themeToggle"' in html
    assert 'class="card__body allowlist-section"' in html


def test_domain_gaps_is_the_only_current_admin_navigation_item():
    response = create_app().test_client().get("/admin/domains")
    html = response.get_data(as_text=True)

    assert 'class="nav-link nav-link--current"\n   href="/admin/domains"' in html
    assert 'class="nav-link "\n   href="/admin/vendors"' in html
