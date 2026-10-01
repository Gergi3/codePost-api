# Copyright © 2026 Rutgers, the State University of New Jersey. All rights reserved except as defined by the Rutgers Non-Commercial License, included with this software.
import asyncio
from types import MethodType, SimpleNamespace
from typing import cast

from core.models import Course
from core.services.ai_service import AIService


def _build_service() -> AIService:
    course = SimpleNamespace(
        ai_provider='openai',
        ai_api_key='test-key',
        ai_base_url=None,
        ai_model='gpt-4o-mini',
        ai_use_own_settings=True,
        ai_disabled=False,
        ai_comments_disabled=False,
        organization=None,
    )
    return AIService(course=cast(Course, course), assignment=None)


def _run(coro):
    return asyncio.run(coro)


def test_generate_test_script_java_strips_wrapper_class_and_imports():
    service = _build_service()

    async def _fake_openai(self, system_prompt: str, user_prompt: str, images=(), response_schema=None):
        return ("""```java
import java.util.regex.Pattern;

public class StudentTests {
    @Test(name=\"A\", points=5, description=\"A\")
    public double testA() {
        return 5.0;
    }

    @Test(name=\"B\", points=5, description=\"B\")
    public Object[] testB() {
        return new Object[] {5.0, \"ok\"};
    }
}
```""", 100, 200, 300, 0)

    service._call_openai = MethodType(_fake_openai, service)

    result = _run(
        service.generate_test_script(
            context_file_content='spec',
            context_filename='solution.java',
            target_filename='Main.java',
            target_code='class Main {}',
            language='java',
        )
    )

    assert result.success
    assert '@Test(name="A"' in result.text
    assert '@Test(name="B"' in result.text
    assert 'public class StudentTests' not in result.text
    assert 'import java.util.regex.Pattern;' not in result.text
    assert '```' not in result.text


def test_generate_test_script_java_keeps_method_only_output():
    service = _build_service()

    raw = """@Test(name=\"Only\", points=10, description=\"single\")
public double testOnly() {
    return 10.0;
}
"""

    async def _fake_openai(self, system_prompt: str, user_prompt: str, images=(), response_schema=None):
        return (raw, 50, 100, 150, 0)

    service._call_openai = MethodType(_fake_openai, service)

    result = _run(
        service.generate_test_script(
            context_file_content='spec',
            context_filename='solution.java',
            target_filename='Main.java',
            target_code='class Main {}',
            language='java',
        )
    )

    assert result.success
    assert result.text.strip() == raw.strip()


def test_generate_test_script_non_java_only_strips_markdown_fences():
    service = _build_service()

    async def _fake_openai(self, system_prompt: str, user_prompt: str, images=(), response_schema=None):
        return ("""```python
@test(\"x\", points=1)
def test_x():
    assert 1 == 1
```""", 80, 120, 200, 0)

    service._call_openai = MethodType(_fake_openai, service)

    result = _run(
        service.generate_test_script(
            context_file_content='spec',
            context_filename='solution.py',
            target_filename='main.py',
            target_code='def f(): pass',
            language='python',
        )
    )

    assert result.success
    assert result.text.startswith('@test(')
    assert '```' not in result.text
