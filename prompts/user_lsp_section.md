### Liskov Substitution Principle (LSP) Focus
Look for evidence that this class cannot safely stand in for its base class or interface.
Specifically, check for:
- Overridden methods that raise `NotImplementedError` or contain only `pass` without fulfilling the expected contract.
- Constructors (`__init__`) that fail to initialize the base class (missing `super().__init__`) when it is expected.
- Methods that strengthen preconditions (e.g., rejecting inputs that the base class accepts, or checking `isinstance` on arguments typed as the base class).
- Methods that weaken postconditions (e.g., returning narrower types, `None`, or different units than expected by the base contract).

Do NOT flag:
- `pass` or `NotImplementedError` inside Abstract Base Classes (ABCs) or Protocols.
- Standard validation that does not contradict the base class contract.