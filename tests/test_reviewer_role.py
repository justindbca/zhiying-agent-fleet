"""Independent reviewer worker: claude-reviewer card -> reviewer.md role (fleet copy of the review rules)."""
from cmux_fleet import fleet, profile_launch
from cmux_fleet.agent_registry import AgentRegistry, PACKAGE_DIR
from cmux_fleet.planner import build_dispatch_plan
from test_fleet import fx  # noqa: F401  (fixture used by live)
from test_profile_integration import dispatch
from test_profile_live import live  # noqa: F401

REQUIRED = [
    "复核是为了尽快上线",                        # top principle, verbatim
    "multi-round-review.md", "R-WKF-033",       # sync note to the source rules
    "Read-only",
    "**P0**", "**P1**", "**P2**",
    "Unsure → go down (P2)",
    "P0 even when unsure",                      # real-money safety exception
    "after go-live, in what normal scenario it happens → wrong result",
    "file:line",
    "Legacy", "Out of scope", "Ledger seen",
    "From r2 on: review only the previous fix diff",
    "PASS = zero P0 and zero P1",
    "## This round's issues",                   # report template
    "Done", "Evidence", "Not done", "Blocked",  # base reply format
]


def test_claude_reviewer_card_uses_reviewer_role_and_opus():
    registry = AgentRegistry()
    card = registry.get("claude-reviewer")
    assert (card.cli, card.role_file, card.model) == ("claude", "reviewer.md", "opus")
    assert (PACKAGE_DIR / "agents" / "reviewer.md").is_file()
    launch = profile_launch.resolve_launch(card, registry, {})
    assert launch["overrides"] == {"worker_cli": "claude", "worker_model": "opus"}
    text = launch["role_text"]
    assert text == (registry.directory / "reviewer.md").read_text()
    missing = [item for item in REQUIRED if item not in text]
    assert not missing, missing


def test_other_cards_keep_plain_worker_role():
    registry = AgentRegistry()
    assert registry.get("flashnext-coder").role_file == "worker.md"
    assert registry.get("researcher").role_file == "worker.md"


def test_code_change_plan_sends_dependent_review_to_reviewer_role():
    plan = build_dispatch_plan("implement feature X and add tests")
    assert [(t.id, t.agent_id, t.depends_on) for t in plan.subtasks] == [
        ("implementation", "flashnext-coder", ()),
        ("review", "claude-reviewer", ("implementation",)),
    ]
    assert AgentRegistry().get(plan.subtasks[1].agent_id).role_file == "reviewer.md"


def test_spawned_reviewer_scope_keeps_base_protocol_then_reviewer_role(live):
    dispatch(live, "claude-reviewer")
    live.deliver("worker-1")
    scope = (fleet.agent_cwd(live.cfg, "worker-1") / "CLAUDE.md").read_text()
    base = scope.index("## 3. Reply format")
    role = scope.index("## Assigned profile role")
    assert base < role < scope.index("复核是为了尽快上线")
    assert "{{" not in scope
