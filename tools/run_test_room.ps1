# PowerShell
# 在项目根目录执行。令牌仅通过命令行参数传入，不写入文件。
$server = "https://10.240.169.190:18080"
$tokens = @(
  $env:MJ_TOKEN_1,
  $env:MJ_TOKEN_2,
  $env:MJ_TOKEN_3,
  $env:MJ_TOKEN_4
)
if ($tokens | Where-Object { [string]::IsNullOrWhiteSpace($_) }) {
  throw "请先设置 MJ_TOKEN_1 到 MJ_TOKEN_4 环境变量"
}
python tools/run_test_room.py @tokens --server $server
