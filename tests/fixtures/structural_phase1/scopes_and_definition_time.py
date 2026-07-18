def helper() -> object:
    return object()


def decorate(value: object) -> object:
    return value


@decorate(helper())
def outer(value: object = helper(), item: object = helper()) -> object:
    def middle() -> object:
        class Inner:
            def method(self) -> object:
                return helper()

        def deepest() -> object:
            return helper()

        return Inner().method(), deepest()

    return middle()
