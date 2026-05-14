def check_duplicate_thought(current_description: str, prev_description: str) -> str | None:
    """Check if the agent is submitting the exact same description for an action."""
    if current_description == prev_description and current_description:
        return (
            "SYSTEM: You have submitted the exact same description as the previous turn — "
            "word for word. You are looping. Either:\n"
            "1. Switch to a different mode for a fresh perspective, or\n"
            "2. Tell the user what you've found so far and ask for guidance.\n"
            "Do NOT repeat the same action."
        )
    return None


def check_repeated_execution(script_to_run: str, exec_history: list[tuple[str, str, int]]) -> tuple[bool, str | None]:
    """Check if the exact same script has been executed multiple times."""
    repeat_count = 0
    matched_cell = None

    script_to_run = script_to_run.strip()
    for prev_script, _prev_output, prev_cell in exec_history:
        if script_to_run == prev_script:
            repeat_count += 1
            matched_cell = prev_cell

    if repeat_count >= 2 and matched_cell is not None:
        warning = (
            f"SYSTEM: This exact script has already been executed {repeat_count} times "
            f"with the same output. It was NOT executed again. "
            f"Use %view_output Cell_{matched_cell} to review the previous output. "
            f"Try a fundamentally different approach."
        )
        return True, warning

    return False, None


def check_final_script_bounce(current_mode: str, final_script_bounces: int, max_bounces: int) -> tuple[bool, str | None]:
    """Handle final script submission constraints."""
    if current_mode != "building":
        return True, (
            "Hey, it seems you are trying to finish the script while not in building mode. "
            "If stuck, or the output isn't working, switch to reading or testing mode "
            "as you wish. We have only one True RULE: Truth and truth alone."
        )

    if (
        final_script_bounces < max_bounces
    ):  # Note: final_script_bounces has already been incremented before this check in main loop
        return True, (
            "Hi there, looks like you have created the final script. "
            "I just came here to verify if you have actually tested it or not. "
            "In case the script isn't running, don't worry, just go back to "
            "reading mode or testing mode. They will take care of the validity. "
            "If test and read modes actually say they can't find any way "
            "to make this work, then you can yield before the user that you "
            "can't find any solution by writing that in normal text and halting. "
            "\nIf you have already tested it, then just submit it again."
        )

    return False, None
