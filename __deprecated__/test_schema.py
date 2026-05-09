import pytest
from pydantic import ValidationError

from automatiq.core.schema import AssistantResponse, Message, PythonScript, ToolEnum


def test_thought_process_validation():
    """Verify that thought_process enforces minimum length and content constraints."""

    # Valid thought process
    resp = AssistantResponse(
        thought_process="I have investigated the DOM and I am now going to extract the table rows.",
        tool=ToolEnum.message_to_user,
        tool_content=Message(message_to_user="Done", does_it_contain_the_final_script=False),
    )
    assert resp.thought_process == "I have investigated the DOM and I am now going to extract the table rows."

    # Empty thought process
    with pytest.raises(ValidationError) as exc:
        AssistantResponse(
            thought_process="",
            tool=ToolEnum.message_to_user,
            tool_content=Message(message_to_user="Done", does_it_contain_the_final_script=False),
        )
    assert "thought_process cannot be empty" in str(exc.value)

    # Whitespace thought process
    with pytest.raises(ValidationError) as exc:
        AssistantResponse(
            thought_process="   \n   ",
            tool=ToolEnum.message_to_user,
            tool_content=Message(message_to_user="Done", does_it_contain_the_final_script=False),
        )
    assert "thought_process cannot be empty" in str(exc.value)

    # Too short thought process (< 40 chars)
    with pytest.raises(ValidationError) as exc:
        AssistantResponse(
            thought_process="I will run code.",
            tool=ToolEnum.message_to_user,
            tool_content=Message(message_to_user="Done", does_it_contain_the_final_script=False),
        )
    assert "thought_process is too short" in str(exc.value)


def test_python_script_syntax_validation():
    """Verify that the PythonScript model correctly validates IPython syntax."""

    # Valid standard Python
    script = PythonScript(ipython_script="x = 10\nprint(x)")
    assert script.ipython_script == "x = 10\nprint(x)"

    # Valid IPython magics
    script = PythonScript(ipython_script="!ls -la\n%timeit [x**2 for x in range(10)]")
    assert "!ls -la" in script.ipython_script

    # Custom AutomatiQ magics bypass standard validation
    script = PythonScript(ipython_script="%reset\nx=1")
    assert "%reset\nx=1" == script.ipython_script

    # Invalid Python syntax
    with pytest.raises(ValidationError) as exc:
        PythonScript(ipython_script="if True\n    print('missing colon')")
    assert "Syntax error: the IPython code is invalid" in str(exc.value)

    # Incomplete blocks (unclosed parentheses)
    with pytest.raises(ValidationError) as exc:
        PythonScript(ipython_script="x = (1, 2, 3")
    assert "Incomplete code: the cell expects more lines" in str(exc.value)

    # Incomplete blocks (unclosed string)
    with pytest.raises(ValidationError) as exc:
        PythonScript(ipython_script='print("""hello')
    assert "Incomplete code: the cell expects more lines" in str(exc.value)

    # Deep syntax error caught by compile()
    with pytest.raises(ValidationError) as exc:
        # Valid structure to the transformer, but invalid Python execution semantics
        PythonScript(ipython_script="return 5")
    assert "Syntax error: the IPython code is invalid" in str(exc.value)
