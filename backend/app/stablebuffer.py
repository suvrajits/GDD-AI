class StableBuffer:
    """
    Removes flicker from Azure partial transcripts.
    Keeps a stable, confirmed prefix and a live, unstable tail.
    """

    def __init__(self):
        self.stable_text = ""
        self.last_partial = ""

    def update_partial(self, partial: str):
        """
        Called on every partial result from Azure.
        Returns the smooth, non-flickering text.
        """

        # Find stable prefix
        prefix_len = 0
        for i in range(min(len(self.last_partial), len(partial))):
            if self.last_partial[i] == partial[i]:
                prefix_len += 1
            else:
                break

        # Update stable prefix
        self.stable_text += self.last_partial[prefix_len:]

        # Save new partial
        self.last_partial = partial

        return self.stable_text + partial

    def commit_final(self, final_text: str):
        """
        Called when Azure sends a final recognized sentence.
        """
        # Add final chunk to stable text
        self.stable_text += final_text + " "
        self.last_partial = ""
        return self.stable_text
