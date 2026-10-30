"""
title: Agent Tasks
description: Постановка задач в оркестратор агентной сети из чата Open WebUI
version: 1.0
"""
import json
import urllib.request
from pydantic import BaseModel, Field


class Tools:
    class Valves(BaseModel):
        ORCH_URL: str = Field(default="http://host.docker.internal:8500")
        ORCH_KEY: str = Field(default="")  # X-Orch-Key из /etc/orchestrator.env (вставляет админ)

    def __init__(self):
        self.valves = self.Valves()

    def _post(self, path, body):
        req = urllib.request.Request(self.valves.ORCH_URL + path, data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json", "X-Orch-Key": self.valves.ORCH_KEY})
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.load(r)

    def _get(self, path):
        req = urllib.request.Request(self.valves.ORCH_URL + path,
            headers={"X-Orch-Key": self.valves.ORCH_KEY})
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.load(r)

    def create_task(self, template: str, file_name: str = "", question: str = "",
                    requester: str = "openwebui") -> str:
        """
        Создать задачу в агентной сети. Шаблоны: doc-convert (файл из kb-inbox → MD + индекс),
        doc-qa (вопрос по корпусу документов с цитатами), echo (тест).
        :param template: имя шаблона задачи
        :param file_name: для doc-convert — имя файла в /srv/agents/kb-inbox (без пути)
        :param question: для doc-qa — текст вопроса по корпусу
        :param requester: кто ставит задачу (email из чата подставится автоматически)
        :return: номер задачи и её статус-ссылка
        """
        payload = {}
        if file_name: payload["file"] = file_name
        if question: payload["question"] = question
        r = self._post("/api/tasks", {"template": template, "payload": payload,
                                      "requester": requester, "group": "everyone"})
        return f"Задача #{r['id']} создана (шаблон {template}). Статус: спросите task_status({r['id']})."

    def task_status(self, task_id: int) -> str:
        """
        Статус задачи оркестратора: шаги, события, артефакты.
        :param task_id: номер задачи
        :return: человекочитаемое состояние
        """
        d = self._get(f"/api/tasks/{task_id}")
        t = d["task"]
        lines = [f"Задача #{t['id']} [{t['template']}] статус: {t['status']}"]
        for s in d["steps"]:
            lines.append(f"  шаг {s['idx']} {s['role']}: {s['status']} { (s['output'] or '')[:120] }")
        for e in d["events"][-5:]:
            lines.append(f"  • {e['message'][:140]}")
        if t.get("artifacts"):
            lines.append("  артефакты: " + ", ".join(t["artifacts"]))
        return "\n".join(lines)
