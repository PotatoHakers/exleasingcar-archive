# Exleasingcar Archive

`Локальный архив публичных данных Exleasingcar.`

## GITHUB

    - #git branch - список веток
    - ##git branch -r - список веток в удаленном репозитории
    - git branch -vv - посмотреть ветки и их связь
        (Показывает:
         - Название ветки.
         - Последний коммит.
         - Связанную удалённую ветку.
         - Статус относительно удалённой ветки.)
    - git status - сравнение текущих файлов с последним коммитом в текущей ветке
        (Показывает:
         - Текущую ветку.
         - Изменённые файлы.
         - Новые файлы.
         - Файлы, добавленные через git add.)
    - git fetch origin - обновляет локальную информацию о состоянии удаленного репозитория origin 
        (fetch получает изменения и обновляет локальные ссылки на удалённые ветки согласно настройкам репозитория.
         При этом:
         - Текущая ветка не переключается.
         - Твои рабочие файлы не изменяются.
         - Локальные коммиты не объединяются с удалёнными.)
    - git pull - получает новые изменения из удаленной ветки
    - git pull origin master - получает изменения именно из удалённого master и применяет их к текущей ветке
    - git config --get-all remote.origin.fetch - проверить настройку fetch
    - git switch -c feature/my-feature - создает новую ветку от текущей ветки
    - git switch -c feature/my-feature master - создает новую ветку от конкретной ветки (master - локальная ветка)
    - git fetch origin & git switch -c feature/my-feature origin/master - cоздатет новую ветку от актуального удалённого master
        (1. git fetch origin — обновляет локальную информацию об удалённом репозитории.
         2. git switch -c — создаёт новую локальную ветку.
         3. origin/master — указывает, от какого коммита создавать новую ветку.
         4. git переключает тебя на новую ветку.)
    - git diff - посмотреть разницу в коде
    - git add . - добавить все новые изменения
    - git diff --cached - посмотреть подготовленные изменения
    - git commit -m "feat: add new functionality" - создать коммит
    - git log --oneline -5 - посмотреть последние коммиты
    - git push -u origin feature/my-feature - опубликовать ветку
    - git log origin/master..HEAD --oneline - посмотреть коммиты текущей ветки которых нет в удаленном master
    - git diff origin/master...HEAD - показывает изменения твоей ветки относительно общей точки, от которой она разошлась с origin/master
    - git branch -d feature/my-feature - удалить локальную ветку
    - git push origin --delete feature/my-feature - удалить удаленную ветку с github

## STASH

    - git stash list - посмотреть все сохранения
    - git stash show -p - посмотреть сохраненный код
    - git stash apply - вернуть сохранения сохранив Stash
    - git stash pop - вернуть изменения и удалить Stash
    - git stash drop - удалить конкретное сохранение
    - git stash clear - удалить все соханения
    - git stash branch - создать ветку и перенести в неё Stash
    - git stash push -u -m "..." - сохранить изменения и новые файлы
    - git pull - обновить текущую ветку
    - git stash pop - вернуть свои именения

## WORKTREE

    - git worktree list - посмотреть все рабочие папки
    - git worktree add (создаёт новую рабочую папку) -b (создаёт новую локальную ветку) feature/my-feature (название ветки) "Путь" origin/master (от какого коммита создать ветку) - создать отдельную рабочую папку
    - git worktree add "Путь" feature/my-feature - создать рабочую папку для уже существующей локальной ветки
    - git worktree add --detach "C:/Users/mix123/projects/test-commit" abc123 - создать рабочу папку для конкретного коммита
    - git -C "Путь" status - проверить состояние папки
    - git worktree remove "Путь" - удалить папку
    - git worktree prune - даляет устаревшие записи о Worktrees, которых больше нет на диске
