import os
import glob


_PROJECT_ROOT_NAME = "pipecat"


def get_project_root():

    # 获取当前所在文件的路径
    cur_path = os.path.abspath(os.path.dirname(__file__))

    # 获取根目录
    return cur_path[:cur_path.find(_PROJECT_ROOT_NAME)] + _PROJECT_ROOT_NAME


def parse_path(path: str) -> str:
    """在配置文件中指定路径的时候可以用{project_root}开头来指定项目内的相对路径."""
    if path.startswith("{project_root}/"):
        project_root = get_project_root()
        path = path.replace("{project_root}/", "")
        path = path.split("/")
        return os.path.join(project_root, *path)
    else:
        return path


def delete_file_by_prefix(prefix: str):
    # 使用glob模块找到所有前缀开头的文件
    files = glob.glob(f"{prefix}*")

    # 遍历文件列表，并删除每个文件
    for file in files:
        try:
            os.remove(file)
            print(f"Deleted: {file}")
        except Exception as e:
            print(f"Failed to delete {file}: {e}")
