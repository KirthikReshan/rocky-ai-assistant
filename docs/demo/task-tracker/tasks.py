"""Small in-memory task tracker used as a portfolio sample."""
from dataclasses import dataclass

@dataclass
class Task:
    title: str
    completed: bool = False

class TaskTracker:
    def __init__(self):
        self.tasks = []

    def add(self, title):
        if not title.strip():
            raise ValueError("A task needs a title")
        task = Task(title.strip())
        self.tasks.append(task)
        return task

    def complete(self, index):
        self.tasks[index].completed = True

    def pending(self):
        return [task for task in self.tasks if not task.completed]
