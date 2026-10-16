-- One-time fixture in a newly created owned demo database. No DROP/reset.
CREATE TABLE dbo.Customers (
 TenantId int NOT NULL, CustomerId int NOT NULL, Name nvarchar(80) NOT NULL,
 CONSTRAINT PK_Customers PRIMARY KEY (TenantId,CustomerId));
CREATE TABLE dbo.Orders (
 TenantId int NOT NULL, OrderId int NOT NULL, CustomerId int NOT NULL,
 Amount decimal(19,4) NOT NULL, Note nvarchar(max),
 CONSTRAINT PK_Orders PRIMARY KEY (TenantId,OrderId),
 CONSTRAINT FK_Orders_Customers FOREIGN KEY(TenantId,CustomerId)
 REFERENCES dbo.Customers(TenantId,CustomerId));
CREATE INDEX IX_Orders_Customer ON dbo.Orders(TenantId,CustomerId);
INSERT INTO dbo.Customers VALUES(1,1,N'測試客戶'),(2,1,N'另一租戶');
INSERT INTO dbo.Orders VALUES(1,1,1,123.4500,N'第一筆訂單'),(1,2,1,2.5000,N'第二筆訂單'),(2,1,1,987654321012345.6789,N'精確金額');
GO
CREATE VIEW dbo.OrderSummary AS SELECT TenantId,SUM(Amount) AS TotalAmount FROM dbo.Orders GROUP BY TenantId;
GO
