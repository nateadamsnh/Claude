# Registers the daily auto-buy task for Consistent-History Losers.
# Run this yourself (in an elevated or normal PowerShell) when you're ready
# to turn on autonomous trading. Not run or enabled automatically.
#
# Schedule: Mon-Fri 9:25 AM ET (5 min before the 9:30 open), so the market
# order gets submitted just ahead of the bell.

$xml = @'
<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Author>TAHOE\Nathaniel</Author>
    <URI>\Alpaca\BuyConsistentLosers</URI>
  </RegistrationInfo>
  <Principals>
    <Principal id="Author">
      <UserId>S-1-5-21-897672513-2981516108-3790960897-1001</UserId>
      <LogonType>InteractiveToken</LogonType>
    </Principal>
  </Principals>
  <Settings>
    <DisallowStartIfOnBatteries>true</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>true</StopIfGoingOnBatteries>
    <Enabled>true</Enabled>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <IdleSettings>
      <Duration>PT10M</Duration>
      <WaitTimeout>PT1H</WaitTimeout>
      <StopOnIdleEnd>true</StopOnIdleEnd>
      <RestartOnIdle>false</RestartOnIdle>
    </IdleSettings>
  </Settings>
  <Triggers>
    <CalendarTrigger>
      <StartBoundary>2026-08-03T09:25:00</StartBoundary>
      <ScheduleByWeek>
        <WeeksInterval>1</WeeksInterval>
        <DaysOfWeek>
          <Monday />
          <Tuesday />
          <Wednesday />
          <Thursday />
          <Friday />
        </DaysOfWeek>
      </ScheduleByWeek>
    </CalendarTrigger>
  </Triggers>
  <Actions Context="Author">
    <Exec>
      <Command>C:\Users\Nathaniel\AppData\Local\Programs\Python\Python310\pythonw.exe</Command>
      <Arguments>C:\Users\Nathaniel\Documents\Trading\scripts\buy_consistent_losers.py</Arguments>
    </Exec>
  </Actions>
</Task>
'@
$xmlPath = "$env:TEMP\buy_consistent_losers_task.xml"
$xml | Out-File -FilePath $xmlPath -Encoding Unicode
schtasks /create /tn "\Alpaca\BuyConsistentLosers" /xml $xmlPath /f
