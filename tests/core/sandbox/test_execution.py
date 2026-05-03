from automatiq.core.ipython_sandbox import AgentSandbox


def test_basic_execution(sandbox: AgentSandbox):
    result = sandbox.execute('print("hello world")')
    assert "Status: Success" in result
    assert "hello world" in result


def test_state_persistence(sandbox: AgentSandbox):
    sandbox.execute("x = 42")
    result = sandbox.execute("print(x)")
    assert "Status: Success" in result
    assert "42" in result


def test_execution_error(sandbox: AgentSandbox):
    result = sandbox.execute("1 / 0")
    assert "Status: ERROR" in result
    assert "ZeroDivisionError" in result


def test_syntax_error(sandbox: AgentSandbox):
    """Test handling of native SyntaxError which occurs before execution"""
    result = sandbox.execute("this is not valid python")
    assert "Status: ERROR" in result
    assert "SyntaxError" in result


def test_magic_reset(sandbox: AgentSandbox):
    sandbox.execute("a = 100")
    sandbox.execute("%reset")
    result = sandbox.execute("print(a)")
    assert "Status: ERROR" in result
    assert "NameError" in result


def test_magic_restore(sandbox: AgentSandbox):
    sandbox.execute("important_var = 'saved'")
    # Simulate a crash that wiped the in-memory kernel state
    sandbox.process.terminate()
    sandbox.process.join()
    sandbox.execute("%restore")
    result = sandbox.execute("print(important_var)")
    assert "Status: Success" in result
    assert "saved" in result


def test_shell_command(sandbox: AgentSandbox):
    """Test shell command via ! escapes"""
    # Use echo, should work on both Windows (jailed busybox) and POSIX
    result = sandbox.execute("!echo foo")
    assert "Status: Success" in result
    assert "foo" in result.lower()


def test_shell_command_error(sandbox: AgentSandbox):
    """Test shell command that fails"""
    result = sandbox.execute("!some_nonexistent_binary_123")
    # IPython catches the shell error and prints it, but the python cell itself 'succeeded'
    assert "Status: Success" in result
    assert "not found" in result.lower() or "not recognized" in result.lower() or "error" in result.lower()


def test_magic_view_output(sandbox: AgentSandbox):
    # generate many lines
    sandbox.execute("for i in range(150):\n    print(f'Line {i}')")
    # first view (cell 1)
    out1 = sandbox.execute("%view_output Cell_1")
    assert "Line 0" in out1
    assert "Line 100" in out1
    # offset view
    out2 = sandbox.execute("%view_output Cell_1 --offset 100")
    assert "Line 100" in out2
    assert "Line 149" in out2
    assert "Line 0" not in out2
