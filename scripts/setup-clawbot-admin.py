"""Initialize or rotate an independent admin password in an owned Linux runtime."""
import argparse
import importlib.util
import json
import os
from pathlib import Path
import secrets
import sys

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0,str(PROJECT))
# Loading app.__init__ would import unrelated dotenv configuration; this standalone module has no app imports.
spec=importlib.util.spec_from_file_location('admin_auth',PROJECT/'app/admin_auth.py')
auth=importlib.util.module_from_spec(spec);spec.loader.exec_module(auth)
spec=importlib.util.spec_from_file_location('setup',PROJECT/'scripts/setup-clawbot.py')
setup=importlib.util.module_from_spec(spec);spec.loader.exec_module(setup)


def configure(root: Path, *, rotate=False):
    if not root.is_absolute() or root.is_symlink() or root!=root.resolve():
        raise ValueError('owned absolute runtime required')
    private=root/'state/xiaoman'
    if (private/'.xiaoman-bridge').read_text().strip() not in ('xiaoman-server-runtime-v1','xiaoman-bridge-v1'):
        raise ValueError('owned runtime required')
    config=json.loads((root/'state/openclaw.json').read_text())
    bridge=config.get('plugins',{}).get('entries',{}).get('xiaoman-bridge',{})
    if not bridge.get('enabled') or Path(bridge['config']['bindingsFile']).resolve()!=private/'bindings.json':
        raise ValueError('activated owned runtime required')
    target=private/'admin-auth.json'
    if target.is_symlink() or (target.exists() and not rotate):
        raise ValueError('existing admin password not overwritten without --rotate')
    backup=private/'admin-setup-backups'/secrets.token_hex(8)
    backup.mkdir(parents=True,mode=0o700)
    env_path=private/'runtime.env'
    if env_path.is_symlink():
        raise ValueError('environment symlink refused')
    setup.write_private(backup/'runtime.env',env_path.read_text())
    if target.exists():
        setup.write_private(backup/'admin-auth.json',target.read_text())
    password=secrets.token_urlsafe(24)
    setup.write_private(target,json.dumps(auth.password_record(password),indent=2)+'\n',replace=target.exists())
    import re
    value='CLAWBOT_ADMIN_AUTH_FILE='+json.dumps(target.as_posix())
    env=env_path.read_text()
    env=re.sub(r'^CLAWBOT_ADMIN_AUTH_FILE\s*=.*$',value,env,flags=re.MULTILINE) if re.search(r'^CLAWBOT_ADMIN_AUTH_FILE\s*=',env,re.MULTILINE) else env+'\n'+value+'\n'
    setup.write_private(env_path,env,replace=True)
    credentials=private/('admin-login-'+secrets.token_hex(4)+'.md')
    setup.write_private(credentials,'# 小满管理后台私有登录说明\n\n账号：`admin`\n\n管理密码：`'+password+'`\n\n仅限管理员使用，不上传仓库或截图分享。密码独立于微信/模型/桥接密钥。\n登录入口为本项目固定域名的 `/manage`；刷新页面需重新登录，会话一小时有效。\n重置使用 setup-clawbot-admin.py --root 实际运行目录 --rotate，旧会话立即失效。\n')
    return credentials


def main():
    parser=argparse.ArgumentParser(description='初始化独立管理员密码，不启动服务或更改成员授权')
    parser.add_argument('--root',type=Path,required=True);parser.add_argument('--rotate',action='store_true')
    args=parser.parse_args()
    try:
        result=configure(args.root,rotate=args.rotate)
        print('管理员账号已准备；密码未输出。私有登录说明：'+str(result))
        print('首次启用需重启后端和网页代理；重置密码会使旧会话失效。')
    except (OSError,ValueError,KeyError,TypeError):
        print('配置失败；未输出凭据。请核对归属、配置与备份，不删除原文件。')
        return 1
    return 0


if __name__=='__main__':
    raise SystemExit(main())
