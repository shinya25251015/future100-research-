"""Phase 2 情報収集コレクタ。

`base` の register デコレータで kind → コレクタを登録する。
新しい取得方式を足すときは、このパッケージにモジュールを追加して
下の import に 1 行加えるだけでよい。
"""
from . import base, json_api, rss  # noqa: F401  (import 副作用で kind を登録する)

__all__ = ["base", "json_api", "rss"]
