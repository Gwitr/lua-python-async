-- This file isn't needed to use the library

function table_init()
    local t = create_example_table()
    
    print("pre modify")
    for k, v in pairs(t) do
        print(k, v)
    end
    
    t["hello"] = "world"
    t:modify_example_table()
    
    print("post modify")
    for k, v in pairs(t) do
        print(k, v)
    end
end

function main()
    local t1 = create_task(task, 0, 1, "[task 1] hello")
    local t2 = create_task(task, 0.25, 1, "[task 2] hello")
    while true do
        if is_task_done(t1) and is_task_done(t2) then
            break
        end
        print("Mainloop")
        wait(.25)
    end
    print("Task 2 returned " .. tostring(join_task(t2)))
    print("Task 1 returned " .. tostring(join_task(t1)))
end

function task(first_delay, delays, text)
    wait(first_delay)
    for var=0,4 do
        print(text)
        wait(delays)
    end
    return "done"
end