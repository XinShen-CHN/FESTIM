class Stepsize:
    """
    A class for evaulating the stepsize of transient simulations.

    Args:
        initial_value (float, int): initial stepsize.

    Attributes:
        initial_value (float, int): initial stepsize.
    """

    def __init__(
        self,
        initial_value,
    ) -> None:
        self.initial_value = initial_value
