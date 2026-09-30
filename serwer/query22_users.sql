-- Użytkownik z największą liczbą spakowanych pozycji w zakończonym dokumencie typu 7.
SELECT TOP (10)
       COALESCE(CU.UserName, CONVERT(nvarchar(255), DD.ModifiedBy)) AS UserName,
       SUM(PS.PackagedPositionCount) AS PackagedPositionCount,
       DD.Id
FROM [SerwisKop_Magazyn].[Document].[Documents] DD
LEFT JOIN Core.Users CU ON CU.Id = TRY_CONVERT(int, DD.ModifiedBy)
INNER JOIN [SerwisKop_Magazyn].[Package].[PackageStats] PS ON PS.DocumentId = DD.Id
WHERE DD.DateCreatedUtc >= DATEADD(DAY, -60, GETUTCDATE())
  AND DD.OriginalNumber = ?
  AND DD.SubType = 50
  AND DD.DocumentType = 7
  AND DD.DocumentStatusText = 'end'
  AND COALESCE(CU.UserName, CONVERT(nvarchar(255), DD.ModifiedBy)) IS NOT NULL
GROUP BY COALESCE(CU.UserName, CONVERT(nvarchar(255), DD.ModifiedBy)), DD.Id
ORDER BY SUM(PS.PackagedPositionCount) DESC, COALESCE(CU.UserName, CONVERT(nvarchar(255), DD.ModifiedBy))
