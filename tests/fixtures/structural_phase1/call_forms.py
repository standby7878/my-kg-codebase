def bare_target() -> None:
    return None


def factory() -> object:
    return object()


class Base:
    def save(self) -> None:
        return None


class Child(Base):
    def instance(self) -> None:
        return None

    @classmethod
    def class_action(cls) -> None:
        return None

    @classmethod
    def via_cls(cls) -> None:
        cls.class_action()

    def run(self, client: object) -> None:
        bare_target()
        self.instance()
        super().save()
        client.send()
        factory().run()
