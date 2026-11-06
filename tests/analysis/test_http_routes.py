"""Ticket 27 (layer 2): the HTTP routes the application registers, read from Go source."""

from cmp_scanners.routes import scan_go_routes

MAIN = """package main

func main() {
	mux := http.NewServeMux()
	mux.HandleFunc("GET /healthz", s.healthz)
	mux.HandleFunc("POST /api/messages", s.postMessage)
	mux.HandleFunc("GET /api/messages", s.listMessages)
	mux.Handle("GET /", http.FileServerFS(static))
	// mux.HandleFunc("POST /api/old", s.old)
	mux.HandleFunc("/legacy", s.legacy)
}
"""


def test_routes_come_with_method_path_file_and_line(tmp_path):
    (tmp_path / "main.go").write_text(MAIN)
    routes = scan_go_routes(tmp_path)
    found = {(r.method, r.path) for r in routes}
    assert ("POST", "/api/messages") in found and ("GET", "/healthz") in found
    assert ("GET", "/") in found
    post = next(r for r in routes if r.path == "/api/messages" and r.method == "POST")
    assert post.file == "main.go" and post.line == 6


def test_a_commented_out_route_is_not_a_route_and_a_route_without_method_has_none(tmp_path):
    (tmp_path / "main.go").write_text(MAIN)
    routes = scan_go_routes(tmp_path)
    assert "/api/old" not in {r.path for r in routes}
    assert next(r for r in routes if r.path == "/legacy").method is None


def test_tests_and_vendored_code_are_not_scanned(tmp_path):
    (tmp_path / "vendor").mkdir()
    (tmp_path / "vendor" / "x.go").write_text('mux.HandleFunc("POST /vendored", h)\n')
    (tmp_path / "main_test.go").write_text('mux.HandleFunc("POST /in-test", h)\n')
    assert scan_go_routes(tmp_path) == []


def test_the_result_is_stable_and_sorted(tmp_path):
    (tmp_path / "b.go").write_text('mux.HandleFunc("POST /b", h)\n')
    (tmp_path / "a.go").write_text('mux.HandleFunc("POST /a", h)\n')
    assert [r.path for r in scan_go_routes(tmp_path)] == ["/a", "/b"]
