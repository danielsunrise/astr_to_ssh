import os
import sys
import paramiko
from mcp.server.fastmcp import FastMCP

# 初始化 MCP 服务
# dependencies: "mcp[cli]", "paramiko"
mcp = FastMCP("SSH_Manager")

def get_ssh_config():
    """从环境变量获取配置"""
    host = os.environ.get("SSH_HOST")
    port = int(os.environ.get("SSH_PORT", 22))
    user = os.environ.get("SSH_USER", "root")
    password = os.environ.get("SSH_PASSWORD")
    
    if not host or not password:
        raise ValueError("❌ 错误: 环境变量 SSH_HOST 和 SSH_PASSWORD 未设置")
    
    return host, port, user, password

@mcp.tool()
def execute_command(command: str) -> str:
    """
    Execute a shell command on the remote server via SSH.
    
    注意：
    1. 这是一个无状态执行工具。这意味着 'cd /tmp' 这种命令不会影响下一条命令。
    2. 如果需要组合操作，请在一个命令中用 '&&' 连接，例如: 'cd /var/www && ls -la'
    
    Args:
        command: The shell command to execute (e.g., 'ls -la', 'docker ps', 'uptime').
    """
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    
    try:
        host, port, user, password = get_ssh_config()
        
        # 建立连接，设置由外层传入的参数
        client.connect(hostname=host, port=port, username=user, password=password, timeout=15)
        
        # 执行命令
        stdin, stdout, stderr = client.exec_command(command)
        
        # 获取退出状态码（阻塞直到命令结束）
        exit_status = stdout.channel.recv_exit_status()
        
        # 读取输出
        out_str = stdout.read().decode('utf-8', errors='replace').strip()
        err_str = stderr.read().decode('utf-8', errors='replace').strip()
        
        result_parts = []
        result_parts.append(f"🔌 Command: `{command}`")
        
        if out_str:
            result_parts.append(f"--- STDOUT ---\n{out_str}")
        if err_str:
            result_parts.append(f"--- STDERR ---\n{err_str}")
        
        if exit_status != 0:
            result_parts.append(f"\n⚠️ Exit Code: {exit_status}")
            
        if not out_str and not err_str:
            result_parts.append("Success (No Output)")

        return "\n".join(result_parts)

    except Exception as e:
        return f"❌ SSH Connection Error: {str(e)}"
    finally:
        client.close()

if __name__ == "__main__":
    mcp.run()
