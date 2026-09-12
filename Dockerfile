FROM golang:1.22 AS build
WORKDIR /src
COPY go.mod .
RUN go mod download
COPY . .
RUN go build -o /out/server .
FROM golang:1.22-alpine
COPY --from=build /out/server /server
EXPOSE 8080
ENTRYPOINT ["/server"]
